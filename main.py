# main.py
import os
from typing import List, Dict, Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from datetime import datetime

APP_VERSION = "2025-09-10.1"

# ========================
# Environment / Config
# ========================
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

_allowed = os.getenv("ALLOWED_ORIGINS", "*").strip()
if not _allowed or _allowed == "*":
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _allowed.split(",") if o.strip()]

# ========================
# FastAPI App + CORS
# ========================
app = FastAPI(title="xau-scanner", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========================
# Models
# ========================
class SignalRequest(BaseModel):
    symbol: str = Field(..., examples=["XAU/USD"])
    tf: str = Field(..., description="M5 or M15", examples=["M5", "M15"])

    @field_validator("tf")
    @classmethod
    def check_tf(cls, v: str) -> str:
        v = v.upper()
        if v not in {"M5", "M15"}:
            raise ValueError("tf must be M5 or M15")
        return v


class Candle(BaseModel):
    dt: str
    open: float
    high: float
    low: float
    close: float


# ========================
# Utilities
# ========================
def td_interval(tf: str) -> str:
    m = tf.upper()
    mapping = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
    }
    if m not in mapping:
        raise ValueError(f"Unsupported TF: {tf}")
    return mapping[m]


def fetch_series(symbol: str, tf: str, size: int = 120) -> List[Candle]:
    """
    ดึงแท่งจาก TwelveData (เรียงจากล่าสุด -> เก่าสุด)
    """
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",      # ล่าสุดก่อน
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"TwelveData unreachable: {e}")

    # บางครั้ง TwelveData จะส่ง HTML เมื่อ rate limit -> กันเอาไว้
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON.")

    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(status_code=502, detail=str(data.get("message", "API error")))
    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData.")

    candles: List[Candle] = []
    for v in values:
        try:
            candles.append(
                Candle(
                    dt=v["datetime"],
                    open=float(v["open"]),
                    high=float(v["high"]),
                    low=float(v["low"]),
                    close=float(v["close"]),
                )
            )
        except Exception:
            # ข้ามแถวที่พาร์สไม่ได้
            continue

    if not candles:
        raise HTTPException(status_code=502, detail="Cannot parse bars.")
    return candles  # ล่าสุดอยู่ index 0


def crossed_above(prev_close: float, last_close: float, level: float) -> bool:
    # ปิดก่อนหน้า ≤ เส้น < ปิดล่าสุด
    return prev_close <= level < last_close


def crossed_below(prev_close: float, last_close: float, level: float) -> bool:
    # ปิดก่อนหน้า ≥ เส้น > ปิดล่าสุด
    return prev_close >= level > last_close


def near(value: float, target: float, tolerance_points: float) -> bool:
    return abs(value - target) <= tolerance_points


# ========================
# Core Logic
# ========================
def analyze_signal(symbol: str, low_tf: str) -> Dict[str, Any]:
    """
    จับคู่ TF ต่ำกับ TF สูง:
      - M5  -> H1
      - M15 -> H4

    ใช้ High/Low ของแท่ง TF สูงที่ปิดล่าสุดเป็นกรอบ (upper/lower),
    แล้วเฝ้าที่ TF ต่ำเพื่อตรวจ breakout แบบ close cross
    """
    low_tf = low_tf.upper()
    high_tf = "H1" if low_tf == "M5" else "H4"

    # --- ดึง TF สูง (แท่งล่าสุดที่ปิด = index 0) ---
    hi_bars = fetch_series(symbol, high_tf, 50)
    ref = hi_bars[0]  # bar อ้างอิง
    upper = ref.high
    lower = ref.low

    # --- ดึง TF ต่ำ 2–3 แท่งล่าสุด ---
    lo_bars = fetch_series(symbol, low_tf, 30)
    if len(lo_bars) < 2:
        return {"status": "ERROR", "message": "Not enough low timeframe data."}
    last = lo_bars[0]
    prev = lo_bars[1]

    # เบรกโดย close cross
    up_break = crossed_above(prev.close, last.close, upper)
    dn_break = crossed_below(prev.close, last.close, lower)

    # ค่าคงที่ RR (จุด)
    sl_points = 250.0
    tp1_points = 500.0
    tp2_points = 1000.0

    # โครงผลลัพธ์ (สั้นกระชับตามที่ขอ) + เก็บ overlay last/prev ไว้ใช้ภายหลังได้
    result: Dict[str, Any] = {
        "status": "OK",
        "symbol": symbol,
        "tf_high": high_tf,
        "tf_low": low_tf,
        "box": {
            "upper": round(upper, 2),
            "lower": round(lower, 2),
            "ref_bar": {
                "dt":   ref.dt,
                "open": round(ref.open,  2),
                "high": round(ref.high,  2),
                "low":  round(ref.low,   2),
                "close":round(ref.close, 2),
            },
        },
        "overlay": {  # ให้ UI เดิมยังดูแท่งล่าสุดได้
            "last": {
                "dt": last.dt, "open": round(last.open,2),
                "high": round(last.high,2), "low": round(last.low,2),
                "close": round(last.close,2),
            },
            "prev": {
                "dt": prev.dt, "open": round(prev.open,2),
                "high": round(prev.high,2), "low": round(prev.low,2),
                "close": round(prev.close,2),
            },
        },
        "signal": None,
        "entry": None,
        "entry_50": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "message": "",
    }

    # ---------- Breakout LONG ----------
    if up_break:
        body = abs(last.close - prev.close)
        entry_level = upper
        tol = max(100.0, 0.5 * body)

        if near(last.close, entry_level, tol):
            result["signal"]  = "ENTRY_READY_LONG"
            result["message"] = f"Retest near {high_tf} resistance — ready to enter long."
        else:
            result["signal"]  = "BREAKOUT_LONG"
            result["message"] = f"Breakout above {high_tf} resistance — wait pullback to enter."

        result["entry"]    = round(entry_level, 2)
        result["entry_50"] = round(entry_level + 0.5 * (last.close - entry_level), 2)
        result["sl"]       = round(entry_level - sl_points, 2)
        result["tp1"]      = round(entry_level + tp1_points, 2)
        result["tp2"]      = round(entry_level + tp2_points, 2)
        return result

    # ---------- Breakout SHORT ----------
    if dn_break:
        body = abs(last.close - prev.close)
        entry_level = lower
        tol = max(100.0, 0.5 * body)

        if near(last.close, entry_level, tol):
            result["signal"]  = "ENTRY_READY_SHORT"
            result["message"] = f"Retest near {high_tf} support — ready to enter short."
        else:
            result["signal"]  = "BREAKOUT_SHORT"
            result["message"] = f"Breakdown below {high_tf} support — wait pullback to enter."

        result["entry"]    = round(entry_level, 2)
        result["entry_50"] = round(entry_level + 0.5 * (last.close - entry_level), 2)
        result["sl"]       = round(entry_level + sl_points, 2)
        result["tp1"]      = round(entry_level - tp1_points, 2)
        result["tp2"]      = round(entry_level - tp2_points, 2)
        return result

    # ---------- ยังไม่เบรก ----------
    result["message"] = f"WAIT — รอราคาเบรกกรอบ {high_tf} บน/ล่าง (ที่ TF {low_tf})."
    return result


# ========================
# Routes
# ========================
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat() + "Z"}


@app.post("/signal")
def signal(req: SignalRequest):
    try:
        return analyze_signal(req.symbol, req.tf)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
