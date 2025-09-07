from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from PIL import Image
import numpy as np

app = FastAPI(title="XAU Scanner API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========= Defaults / Params =========
SL_POINTS_DEFAULT  = 250
TP1_POINTS_DEFAULT = 500
TP2_POINTS_DEFAULT = 1000
EPS                = 0.5   # กัน noise เล็กน้อยเวลาตรวจ break

# ========= Utils (ไม่แตะ logic OCR เดิมของคุณ) =========
def pil_to_gray_arr(pil_img: Image.Image) -> np.ndarray:
    return np.array(pil_img.convert("L"), dtype=np.float32) / 255.0

def simple_brightness_trend(img: Image.Image):
    """ เฉพาะ endpoint /scan-image (เดิม) ไว้ดูแนวโน้มแบบ heuristic """
    g = pil_to_gray_arr(img)
    h, w = g.shape
    center = g[h // 3 : h * 2 // 3, :]
    col_mean = center.mean(axis=0)
    x = np.arange(len(col_mean))
    x = (x - x.mean()) / (x.std() + 1e-8)
    y = (col_mean - col_mean.mean()) / (col_mean.std() + 1e-8)
    slope = float((x * y).mean())
    direction = "UP" if slope > 0.002 else ("DOWN" if slope < -0.002 else "SIDEWAYS")
    return direction, slope, float(col_mean.mean())

# ===== Break + Close Logic (แท่ง H1/H4) =====
def compute_break_close_from_candle(
    h_high: float, h_low: float, last_close: float,
    sl_points: int = SL_POINTS_DEFAULT,
    tp1_points: int = TP1_POINTS_DEFAULT,
    tp2_points: int = TP2_POINTS_DEFAULT,
):
    result = {
        "status": "WATCH",
        "reason": "ยังไม่ Breakout แท่ง H1/H4",
        "ref": {
            "H_high": round(h_high, 2),
            "H_low": round(h_low, 2),
            "last": round(last_close, 2),
        },
        "params": {
            "sl_points": sl_points,
            "tp1_points": tp1_points,
            "tp2_points": tp2_points,
        }
    }

    # LONG: ปิดเหนือไฮ
    if last_close > (h_high + EPS):
        entry = last_close
        sl    = entry - sl_points
        tp1   = entry + tp1_points
        tp2   = entry + tp2_points
        result.update({
            "status": "ENTRY",
            "side": "LONG",
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "reason": f"Close > H_high + {EPS}",
        })
        return result

    # SHORT: ปิดใต้โลว์
    if last_close < (h_low - EPS):
        entry = last_close
        sl    = entry + sl_points
        tp1   = entry - tp1_points
        tp2   = entry - tp2_points
        result.update({
            "status": "ENTRY",
            "side": "SHORT",
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "reason": f"Close < H_low - {EPS}",
        })
        return result

    return result

# ========= Root / Health =========
@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-image (file), POST /scan-breakout (file_m, file_h)"}

@app.get("/health")
def health():
    return {"ok": True}

# ========= เดิม: /scan-image =========
@app.post("/scan-image")
async def scan_image(file: UploadFile = File(...)):
    img = Image.open(file.file).convert("RGB")
    direction, slope, score = simple_brightness_trend(img)
    return {
        "status": "OK",
        "signal": "WAIT",
        "reason": {
            "direction": direction,
            "score": round(score, 6),
            "slope": round(slope, 6),
            "note": "Heuristic from image brightness trend."
        }
    }

# ========= ใหม่: /scan-breakout (Break + Close) =========
@app.post("/scan-breakout")
async def scan_breakout(
    file_m: UploadFile = File(..., description="ภาพ TF ต่ำ (M5/M15)"),
    file_h: UploadFile = File(..., description="ภาพ TF สูง (H1/H4)"),
    higher_tf: str = Form("H4"),
    lower_tf: str  = Form("M15"),
    # อนุญาต override ตัวเลขทาง query/form เผื่อดีบัก
    h_high_override: Optional[float] = Form(None),
    h_low_override: Optional[float]  = Form(None),
    last_override: Optional[float]   = Form(None),
    sl_points: int = Form(SL_POINTS_DEFAULT),
    tp1_points: int = Form(TP1_POINTS_DEFAULT),
    tp2_points: int = Form(TP2_POINTS_DEFAULT),
):
    """
    หมายเหตุสำคัญ:
    - โค้ดนี้ *ไม่ไปแก้* ฟังก์ชัน OCR เดิมของคุณ
    - ถ้าคุณมีฟังก์ชัน extract_higher_box / extract_last_close อยู่แล้ว
      ในโปรเจ็กต์เดิม ให้ import ได้โดยตรง (ห่อ try/except ไว้)
    - ถ้าไม่มี -> ใช้ค่าจาก *_override เพื่อทดสอบชั่วคราว
    """
    # 1) ลองใช้ override ก่อน (สำหรับทดสอบ)
    hh = h_high_override
    ll = h_low_override
    lc = last_override

    # 2) ถ้าไม่มี override พยายามเรียกของเดิม
    try:
        from legacy_extractors import extract_higher_box, extract_last_close
        if hh is None or ll is None:
            img_h = Image.open(file_h.file).convert("RGB")
            hh2, ll2 = extract_higher_box(img_h)   # <- ใช้ของเดิมของคุณ
            hh = hh or hh2
            ll = ll or ll2
        if lc is None:
            img_m = Image.open(file_m.file).convert("RGB")
            lc2 = extract_last_close(img_m)         # <- ใช้ของเดิมของคุณ
            lc  = lc or lc2
    except Exception:
        # ถ้า import ไม่ได้ ให้ข้าม (จะไป required check ด้านล่าง)
        pass

    # 3) ต้องได้ครบ 3 ตัวเพื่อคำนวณ
    if hh is None or ll is None or lc is None:
        return {"detail": "Not Found", "hint": "ต้องได้ H_high, H_low จาก H1/H4 และ last_close จาก M5/M15 (หรือใส่ override)"}

    result = compute_break_close_from_candle(
        h_high=float(hh), h_low=float(ll), last_close=float(lc),
        sl_points=sl_points, tp1_points=tp1_points, tp2_points=tp2_points,
    )
    result["ref"]["higher_tf"] = higher_tf
    result["ref"]["lower_tf"]  = lower_tf
    return result

