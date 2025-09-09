# main.py (backend)
import os
from datetime import datetime
from typing import List, Dict, Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-09.5"

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOW_ORIGINS = ["*"] if not _ALLOWED or _ALLOWED == "*" else [o.strip() for o in _ALLOWED.split(",") if o.strip()]

app = FastAPI(title="xau-scanner")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

def td_interval(tf: str) -> str:
    mapping = {"M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h"}
    if tf.upper() not in mapping:
        raise ValueError(f"Unsupported TF: {tf}")
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

    candles: List[Candle] = []
    for v in values:
        try:
            candles.append(Candle(
                dt=v["datetime"],
                open=float(v["open"]),
                high=float(v["high"]),
                low=float(v["low"]),
                close=float(v["close"]),
            ))
        except Exception:
            continue
    if not candles:
        raise HTTPException(status_code=502, detail="Cannot parse bars.")
    return candles

def previous_closed(candles: List[Candle]) -> Candle:
    return candles[0]

def crossed_above(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close <= level < last_close

def crossed_below(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close >= level > last_close

def near(value: float, target: float, tolerance_points: float) -> bool:
    return abs(value - target) <= tolerance_points

def analyze_signal(symbol: str, low_tf: str) -> Dict[str, Any]:
    h1 = previous_closed(fetch_series(symbol, "H1", 50))
    h4 = previous_closed(fetch_series(symbol, "H4", 50))

    resistance = max(h1.high, h4.high)
    support = min(h1.low, h4.low)

    bars = fetch_series(symbol, low_tf, 30)
    if len(bars) < 3:
        return {"status": "ERROR", "message": "Not enough low timeframe data."}

    last, prev = bars[0], bars[1]

    up_break = crossed_above(prev.close, last.close, resistance)
    dn_break = crossed_below(prev.close, last.close, support)

    sl_points, tp1_points, tp2_points = 250.0, 500.0, 1000.0

    result: Dict[str, Any] = {
        "status": "OK",
        "symbol": symbol,
        "tf": low_tf,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "overlay": {"h1": h1.model_dump(), "h4": h4.model_dump(), "last": last.model_dump(), "prev": prev.model_dump()},
        "signal": None,
        "entry": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "message": "",
    }

    if up_break:
        body = abs(last.close - prev.close)
        entry, tol = resistance, max(100.0, 0.5 * body)
        if near(last.close, entry, tol):
            result.update(signal="ENTRY_READY_LONG", message="Price retesting resistance after breakout.")
        else:
            result.update(signal="BREAKOUT_LONG", message="Breakout above resistance. Wait pullback to enter.")
        result.update(entry=round(entry, 2), sl=round(entry - sl_points, 2),
                      tp1=round(entry + tp1_points, 2), tp2=round(entry + tp2_points, 2))
        return result

    if dn_break:
        body = abs(last.close - prev.close)
        entry, tol = support, max(100.0, 0.5 * body)
        if near(last.close, entry, tol):
            result.update(signal="ENTRY_READY_SHORT", message="Price retesting support after breakout.")
        else:
            result.update(signal="BREAKOUT_SHORT", message="Breakdown below support. Wait pullback to enter.")
        result.update(entry=round(entry, 2), sl=round(entry + sl_points, 2),
                      tp1=round(entry - tp1_points, 2), tp2=round(entry - tp2_points, 2))
        return result

    result.update(signal=None, message="WAIT — รอราคาเบรกกรอบบน/ล่าง (ภายใน 2–3 ชม. ที่ TF ต่ำ).")
    return result

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
