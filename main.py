# main.py (backend v2 - matched with frontend v2)
import os
from datetime import datetime
from typing import List, Optional, Dict, Any, Literal

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-10.v2"

# ===== Config =====
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOW_ORIGINS = ["*"] if not _ALLOWED or _ALLOWED == "" or _ALLOWED == "*" else [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# ===== App =====
app = FastAPI(title="xau-scanner")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Models =====
TF_HIGH = Literal["H1", "H4"]
TF_LOW = Literal["M5", "M15"]

class SignalRequest(BaseModel):
    symbol: str = Field(..., examples=["XAU/USD"])
    tf_high: Optional[TF_HIGH] = None
    tf_low: Optional[TF_LOW] = None
    # backward-compat (ไม่ใช้กับ frontend v2 แต่รองรับไว้)
    tf: Optional[TF_LOW] = None

    @field_validator("tf")
    @classmethod
    def _check_tf_low(cls, v: Optional[str]) -> Optional[str]:
        if v is None: return v
        v = v.upper()
        if v not in {"M5","M15"}:
            raise ValueError("tf must be M5 or M15")
        return v

class Candle(BaseModel):
    dt: str
    open: float
    high: float
    low: float
    close: float

# ===== Utilities =====
def td_interval(tf: str) -> str:
    mapping = {"M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h"}
    return mapping[tf.upper()]

def fetch_series(symbol: str, tf: str, size: int = 120) -> List[Candle]:
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    r = requests.get(url, params=params, timeout=20)
    data = r.json()
    if "status" in data and data["status"] == "error":
        raise HTTPException(status_code=502, detail=str(data.get("message", "API error")))
    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData.")

    out: List[Candle] = []
    for v in values:
        try:
            out.append(Candle(
                dt=v["datetime"],
                open=float(v["open"]),
                high=float(v["high"]),
                low=float(v["low"]),
                close=float(v["close"]),
            ))
        except Exception:
            continue
    if not out:
        raise HTTPException(status_code=502, detail="Cannot parse bars.")
    return out  # latest first

# ===== Box from high TF =====
def build_box(symbol: str, tf_high: str) -> Dict[str, Any]:
    bars = fetch_series(symbol, tf_high, 10)  # latest first
    ref = bars[0]  # previous closed
    return {
        "tf_high": tf_high,
        "ref_bar": ref.model_dump(),
        "upper": float(ref.high),
        "lower": float(ref.low),
    }

def compute_tp_sl(side: str, entry: float, upper: float, lower: float) -> Dict[str, float]:
    width = max(1.0, upper - lower)
    if side == "BUY":
        sl  = lower
        tp1 = entry + width * 1.0
        tp2 = entry + width * 1.5
    else:
        sl  = upper
        tp1 = entry - width * 1.0
        tp2 = entry - width * 1.5
    return {"sl": round(sl,2), "tp1": round(tp1,2), "tp2": round(tp2,2)}

# ===== Core =====
def analyze_signal(symbol: str, tf_low: str, tf_high: str) -> Dict[str, Any]:
    box = build_box(symbol, tf_high)
    upper, lower = box["upper"], box["lower"]

    look = 60 if tf_low == "M5" else 24  # ~2–3 hours
    lows = fetch_series(symbol, tf_low, look)
    last = lows[0]
    prev = lows[1] if len(lows) > 1 else last

    broke_up   = last.close > upper
    broke_down = last.close < lower

    result: Dict[str, Any] = {
        "status": "OK",
        "symbol": symbol,
        "tf_high": tf_high,
        "tf_low": tf_low,
        "box": {"upper": round(upper,2), "lower": round(lower,2), "ref_bar": box["ref_bar"]},
        "overlay": {"last": last.model_dump(), "prev": prev.model_dump()},
        "signal": None, "entry": None, "entry_50": None, "sl": None, "tp1": None, "tp2": None,
        "message": "",
    }

    if broke_up or broke_down:
        side = "BUY" if broke_up else "SELL"
        entry_at_edge = upper if side == "BUY" else lower
        body_mid = (last.open + last.close)/2.0 if last.open != last.close else entry_at_edge
        tp_sl = compute_tp_sl(side, entry_at_edge, upper, lower)

        result.update({
            "signal": f"BREAKOUT_{'LONG' if side=='BUY' else 'SHORT'}",
            "message": "Breakout detected — plan for retest (edge) or 50% pullback.",
            "entry": round(entry_at_edge, 2),
            "entry_50": round(body_mid, 2),
            "sl": tp_sl["sl"], "tp1": tp_sl["tp1"], "tp2": tp_sl["tp2"],
        })
        return result

    result["message"] = "WAIT — รอราคาเบรกกรอบบน/ล่าง (ภายใน 2–3 ชม. ที่ TF ต่ำ)."
    return result

# ===== Routes =====
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat() + "Z"}

@app.post("/signal")
def signal(req: SignalRequest):
    # default: tf_high=H4, tf_low=M15 (และรองรับรูปแบบเก่า tf=M5/M15)
    tf_low = (req.tf_low or req.tf or "M15").upper()
    tf_high = (req.tf_high or "H4").upper()

    if tf_low not in {"M5","M15"}:
        raise HTTPException(status_code=422, detail="tf_low/tf must be M5 or M15")
    if tf_high not in {"H1","H4"}:
        raise HTTPException(status_code=422, detail="tf_high must be H1 or H4")

    return analyze_signal(req.symbol, tf_low, tf_high)
