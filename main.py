# main.py
import io
import os
from typing import Optional

import numpy as np
from fastapi import FastAPI, File, UploadFile, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

# ถ้ามีไฟล์ scanner.py อยู่ใน repo (ตามที่เราทำก่อนหน้า)
# ฟังก์ชัน scan_breakout(symbol, higher_tf, lower_tf, retest_window_L, sl_points, tp1_points, tp2_points)
# จะถูกเรียกใน /scan-market
try:
    from scanner import scan_breakout  # type: ignore
    HAS_SCANNER = True
except Exception:
    HAS_SCANNER = False

app = FastAPI(title="XAU Scanner API", version="1.0.0")

# === CORS (อนุญาตให้เรียกจากหน้าเว็บ Netlify/อื่น ๆ) ===
# ถ้าต้องการล็อคโดเมนให้แทนที่ "*" ด้วยโดเมน Netlify ของคุณ
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # แนะนำ: เปลี่ยนเป็น ['https://<your-site>.netlify.app']
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Health & root ----------
@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-image with form-data 'file'."}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/version")
def version():
    return {"version": app.version}


# ---------- Image Analyzer ----------
def _analyze_image_trend(img: Image.Image) -> dict:
    """
    วิเคราะห์แนวโน้มจากรูปกราฟแบบ heuristic ง่าย ๆ:
    - แปลงเป็นเทา -> resize -> คำนวณค่าเฉลี่ยความสว่างต่อคอลัมน์
    - fit เส้นตรง (polyfit degree=1) หา slope
    - กำหนด signal จาก slope
    """
    # ทำให้ deterministic และไว
    g = img.convert("L").resize((512, 256))
    arr = np.asarray(g, dtype=np.float32) / 255.0  # 0..1 (สว่างมาก = ค่าเยอะ)

    # โฟกัสเฉพาะโซนหลักของกราฟ (ตัดขอบบน/ล่างนิดหน่อยกัน UI เส้นค่า indicator)
    top_cut, bottom_cut = int(0.12 * arr.shape[0]), int(0.88 * arr.shape[0])
    core = arr[top_cut:bottom_cut, :]

    # เฉลี่ยตามแกนแนวนอน (ได้สว่างเฉลี่ยต่อคอลัมน์)
    brightness = core.mean(axis=0)  # shape (W,)

    # ทำ smoothing เล็กน้อย
    k = 9
    if brightness.size > k:
        kernel = np.ones(k) / k
        brightness = np.convolve(brightness, kernel, mode="same")

    # Fit เส้นตรง
    x = np.arange(brightness.size, dtype=np.float32)
    slope, intercept = np.polyfit(x, brightness, 1)

    # normalize slope ให้เทียบกับช่วงสเกล (0..1) และความกว้างภาพ
    norm_slope = float(slope) * brightness.size

    # เกณฑ์ตัดสิน (ปรับได้)
    up_th = 0.015
    down_th = -0.015

    if norm_slope > up_th:
        direction = "UP"
        signal = "BUY"
    elif norm_slope < down_th:
        direction = "DOWN"
        signal = "SELL"
    else:
        direction = "SIDEWAYS"
        signal = "WAIT"

    score = float(abs(norm_slope))

    return {
        "signal": signal,
        "reason": {
            "direction": direction,
            "score": round(score, 6),
            "slope": round(float(norm_slope), 6),
            "note": "Heuristic from image brightness trend.",
        },
    }


@app.post("/scan-image")
async def scan_image(
    file: UploadFile = File(..., description="รูปกราฟ (png/jpg) ในฟอร์ม-คีย์ชื่อ 'file'")
):
    # ตรวจชนิดไฟล์คร่าว ๆ
    if file.content_type not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        raise HTTPException(status_code=415, detail="Unsupported file type")

    try:
        raw = await file.read()
        img = Image.open(io.BytesIO(raw))
    except Exception:
        raise HTTPException(status_code=400, detail="Cannot read image")

    result = _analyze_image_trend(img)
    return {"status": "OK", **result}


# ---------- Market Breakout (ออปชั่น ใช้ถ้ามี scanner.py) ----------
@app.get("/scan-market")
def scan_market(
    symbol: str = Query("XAU/USD"),
    higher_tf: str = Query("1h"),
    lower_tf: str = Query("5min"),
    retest_window_L: int = Query(24, ge=1, le=500),
    sl_points: int = Query(12, ge=1, le=10000),
    tp1_points: int = Query(25, ge=1, le=100000),
    tp2_points: int = Query(50, ge=1, le=100000),
):
    """
    เรียกใช้กลยุทธ์ Breakout จาก scanner.py
    """
    if not HAS_SCANNER:
        raise HTTPException(status_code=501, detail="scanner.py is not available on server.")

    try:
        result = scan_breakout(
            symbol=symbol,
            higher_tf=higher_tf,
            lower_tf=lower_tf,
            retest_window_L=retest_window_L,
            sl_points=sl_points,
            tp1_points=tp1_points,
            tp2_points=tp2_points,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"scan_market error: {e}")
