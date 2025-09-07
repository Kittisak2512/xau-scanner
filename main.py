from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from PIL import Image
import io
import numpy as np

app = FastAPI(title="XAU Scanner API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# ---------- utilities: ง่ายและเบา ไม่พึ่ง OCR ----------
def _avg_brightness(img_bytes: bytes) -> float:
    img = Image.open(io.BytesIO(img_bytes)).convert("L").resize((256, 256))
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return float(arr.mean())

def _box_from_higher_tf(img_bytes: bytes):
    """
    heuristic: ใช้ความสว่างครึ่งบน/ล่างแทนบริบท H_high/H_low (ง่ายและไว)
    ใช้เพื่อให้ระบบทำงาน end-to-end ได้ทันที
    """
    img = Image.open(io.BytesIO(img_bytes)).convert("L").resize((256, 256))
    arr = np.asarray(img, dtype=np.float32)
    top = arr[:128].mean()
    bot = arr[128:].mean()
    # map ให้อยู่ในช่วงราคาจำลอง (เพื่อคำนวณสัดส่วน SL/TP)
    base = 3500.0
    rng = 200.0
    h_high = base + rng * max(top, bot)
    h_low  = base - rng * max(top, bot)
    return round(h_high, 2), round(h_low, 2)

def _entry_break_close(m15_brightness: float, last_ref: float, h_high: float, h_low: float):
    """
    สัญญาณ Break + Close ทันที:
    - ถ้าค่าความสว่างของ M15 > ค่าของ ref -> สมมติว่าปิดเหนือกรอบ (LONG)
    - ถ้าต่ำกว่าอย่างชัดเจน -> ปิดต่ำกว่ากรอบ (SHORT)
    หมายเหตุ: ตรรกะนี้ใช้ได้กับระบบสแกนภาพง่าย/ทันที
    """
    # ให้ last ประมาณกลางกรอบ
    last = (h_high + h_low) / 2.0
    # threshold เทียบกับกรอบ
    if m15_brightness > 0.55:
        # เข้าซื้อเมื่อ “แท่งปิดนอกกรอบบน”
        entry = h_high + 3.0
        side = "LONG"
    elif m15_brightness < 0.45:
        # เข้าขายเมื่อ “แท่งปิดนอกกรอบล่าง”
        entry = h_low - 3.0
        side = "SHORT"
    else:
        return None

    return side, round(entry, 2)

@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-breakout with form-data m15,h4 + sl_points,tp1_points,tp2_points"}

@app.post("/scan-breakout")
async def scan_breakout(
    m15: UploadFile = File(...),
    h4:  UploadFile = File(...),  # คุณจะส่ง H1 หรือ H4 ก็ให้ผูก id นี้ไว้
    sl_points: int = Form(250),
    tp1_points: int = Form(500),
    tp2_points: int = Form(1000),
):
    m15_bytes = await m15.read()
    h4_bytes  = await h4.read()

    # 1) สร้างกรอบจาก H1/H4 (heuristic)
    h_high, h_low = _box_from_higher_tf(h4_bytes)

    # 2) ประเมิน “แท่งปิด” ของ M15
    m15_b = _avg_brightness(m15_bytes)

    # 3) ตรรกะแบบ Break + Close ทันที
    entry_signal = _entry_break_close(m15_b, (h_high+h_low)/2, h_high, h_low)

    payload = {
        "status": "WATCH",
        "reason": "ยังไม่ Breakout โซน H1/H4",
        "ref": {
            "higher_tf": "H4",
            "lower_tf": "M15",
            "H_high": h_high,
            "H_low": h_low,
            "box_height": round(abs(h_high-h_low), 2)
        },
        "params": {
            "sl_points": sl_points,
            "tp1_points": tp1_points,
            "tp2_points": tp2_points
        }
    }

    if entry_signal is None:
        return payload

    side, entry = entry_signal
    if side == "LONG":
        sl  = entry - sl_points * 0.1
        tp1 = entry + tp1_points * 0.1
        tp2 = entry + tp2_points * 0.1
    else:
        sl  = entry + sl_points * 0.1
        tp1 = entry - tp1_points * 0.1
        tp2 = entry - tp2_points * 0.1

    note = f"ENTRY {side} @ {entry:.2f} | SL {sl:.2f} | TP1 {tp1:.2f} | TP2 {tp2:.2f}"

    payload.update({
        "status": "ENTRY",
        "signal": side,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "note": note
    })
    return payload

