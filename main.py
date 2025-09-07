from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import io
from PIL import Image
import numpy as np
import os
import httpx

app = FastAPI()

# --- CORS: อนุญาตให้ Frontend เรียก API ได้ ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],     # ใส่โดเมน Netlify ของคุณแทน "*" ได้
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Health check
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": "2025-09-07.1", "ok": True}

# --------- จากรูปภาพ (เดิม) ----------
class ScanResp(BaseModel):
    status: str
    signal: str
    entry: float | None = None
    sl: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    reason: dict | None = None
    higher_tf: str | None = None
    lower_tf: str | None = None

def _brightness_trend(img: Image.Image):
    gray = img.convert("L")
    arr = np.asarray(gray, dtype=np.float32)
    h, w = arr.shape
    left = arr[:, : w // 2].mean()
    right = arr[:, w // 2 :].mean()
    slope = (right - left) / max(1.0, abs(left) + 1e-6)
    return left, right, slope

@app.post("/scan-breakout", response_model=ScanResp)
async def scan_breakout(
    m15: UploadFile = File(...),
    h4: UploadFile = File(...),
    higher_tf: str = Form(...),
    lower_tf: str = Form(...),
    sl_points: int = Form(250),
    tp1_points: int = Form(500),
    tp2_points: int = Form(1000),
):
    m15_img = Image.open(io.BytesIO(await m15.read()))
    h4_img = Image.open(io.BytesIO(await h4.read()))

    _, _, slope_h4 = _brightness_trend(h4_img)
    direction = "UP" if slope_h4 > 0 else "DOWN"
    signal = "WAIT"
    status = "OK"

    ref = {
        "direction": direction,
        "slope": round(float(slope_h4), 6),
        "note": "Heuristic from image brightness trend.",
    }

    return ScanResp(
        status=status,
        signal=signal,
        reason=ref,
        higher_tf=higher_tf,
        lower_tf=lower_tf,
    )

# --------- จาก TwelveData (สัญญาณจริง + ENTRY/SL/TP) ----------
TWELVE = os.getenv("TWELVEDATA_API_KEY", "")

class SignalReq(BaseModel):
    symbol: str = "XAU/USD"
    higher_tf: str = "H4"   # "H1" / "H4"
    lower_tf: str = "M15"
    sl_points: int = 250
    tp1_points: int = 500
    tp2_points: int = 1000

@app.post("/signal", response_model=ScanResp)
async def signal(req: SignalReq):
    if not TWELVE:
        return ScanResp(status="ERROR", signal="WAIT", reason={"msg": "Missing TWELVEDATA_API_KEY"})

    tf_map = {"M15": "15min", "H1": "1h", "H4": "4h"}
    higher_interval = tf_map.get(req.higher_tf.upper(), "4h")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": req.symbol,
        "interval": higher_interval,
        "outputsize": 200,
        "apikey": TWELVE,
        "format": "JSON",
    }

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        data = r.json()

    if "values" not in data:
        return ScanResp(status="ERROR", signal="WAIT", reason={"api": data})

    values = data["values"][:100][::-1]  # เก่า->ใหม่
    highs = [float(v["high"]) for v in values]
    lows  = [float(v["low"])  for v in values]
    closes= [float(v["close"]) for v in values]

    box_high = max(highs[-10:])
    box_low  = min(lows[-10:])
    last     = closes[-1]

    signal = "WAIT"
    reason = {}
    entry = sl = tp1 = tp2 = None

    # Break + Close ทันที
    if last > box_high:
        signal = "ENTRY_LONG"
        entry = last
        sl = entry - req.sl_points
        tp1 = entry + req.tp1_points
        tp2 = entry + req.tp2_points
        reason = {"why": "Close > H_high", "H_high": box_high, "H_low": box_low, "last": last}
    elif last < box_low:
        signal = "ENTRY_SHORT"
        entry = last
        sl = entry + req.sl_points
        tp1 = entry - req.tp1_points
        tp2 = entry - req.tp2_points
        reason = {"why": "Close < H_low", "H_high": box_high, "H_low": box_low, "last": last}
    else:
        reason = {"why": "ยังไม่ Breakout โซน H1/H4",
                  "H_high": box_high, "H_low": box_low, "last": last}

    return ScanResp(
        status="OK",
        signal=signal,
        entry=entry,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        reason=reason,
        higher_tf=req.higher_tf,
        lower_tf=req.lower_tf,
    )
