# main.py
from fastapi import FastAPI, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
from PIL import Image
import io
import numpy as np
from scanner import scan_breakout

app = FastAPI(title="XAU Scanner API")

# อนุญาต CORS ให้ Netlify
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # ถ้าอยากจำกัด ให้ใส่โดเมน Netlify ของคุณ
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-image with form-data 'file'."}

@app.get("/health")
def health():
    return {"status": "ok"}

# ---------- โหมดสแกนจากภาพ (เดิม) ----------
@app.post("/scan-image")
async def scan_image(file: UploadFile = File(...)):
    """
    อ่านภาพ -> แปลง gray -> เฉลี่ยความสว่างแนวตั้ง -> ดู slope
    heuristic ให้สัญญาณเบื้องต้น (UP/DOWN/SIDEWAYS)
    """
    raw = await file.read()
    img = Image.open(io.BytesIO(raw)).convert("L")
    arr = np.array(img, dtype=float)
    col_mean = arr.mean(axis=0)            # เฉลี่ยตามแนวตั้ง
    x = np.arange(len(col_mean))
    # linear regression slope
    A = np.vstack([x, np.ones_like(x)]).T
    slope, _ = np.linalg.lstsq(A, col_mean, rcond=None)[0]
    score = float(slope / (np.std(col_mean) + 1e-8))

    if score > 0.01:
        direction = "UP"
        signal = "BUY"
    elif score < -0.01:
        direction = "DOWN"
        signal = "SELL"
    else:
        direction = "SIDEWAYS"
        signal = "WAIT"

    return {
        "status": "OK",
        "signal": signal,
        "reason": {
            "direction": direction,
            "score": round(score, 6),
            "slope": round(float(slope), 6),
            "note": "Heuristic from image brightness trend."
        }
    }

# ---------- โหมด Breakout (ใหม่) ----------
@app.get("/scan-breakout")
def scan_breakout_api(
    symbol: str = Query("XAU/USD"),
    higher_tf: str = Query("1h", regex="^(1h|4h)$"),
    lower_tf: str = Query("5min", regex="^(5min|15min)$"),
    sl_points: float = 20,
    tp1_points: float = 25,
    tp2_points: float = 50,
    lookback_h: int = 100
):
    """
    ใช้งาน:
    GET /scan-breakout?symbol=XAU/USD&higher_tf=1h&lower_tf=5min
    """
    result = scan_breakout(
        symbol=symbol,
        higher_tf=higher_tf,
        lower_tf=lower_tf,
        lookback_h=lookback_h,
        sl_points=sl_points,
        tp1_points=tp1_points,
        tp2_points=tp2_points
    )
    return result
