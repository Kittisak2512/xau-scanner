# main.py
import os
import math
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-09.3"

# ==========
# Config
# ==========
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
if not TWELVEDATA_API_KEY:
    # ให้ Deploy ได้ แต่ถ้าเรียก /signal จะ error ที่ runtime แทน
    pass

# ALLOWED_ORIGINS: คั่นด้วยคอมมา หรือใช้ * ก็ได้
_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _ALLOWED == "*" or _ALLOWED == "":
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# ==========
# App
# ==========
app = FastAPI(title="xau-scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========
# Models
# ==========
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


# ==========
# Utilities
# ==========
def td_interval(tf: str) -> str:
    """
    Map timeframe to TwelveData interval string.
    """
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
    Fetch time series from TwelveData. Return list of Candle (latest first).
    """
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
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON.")

    if "status" in data and data["status"] == "error":
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
            # skip bad row
            continue

    if not candles:
        raise HTTPException(status_code=502, detail="Cannot parse bars.")
    return candles  # latest first


def previous_closed(candles: List[Candle]) -> Candle:
    """
    TwelveData typically returns closed bars (desc). Use index 0 as latest closed.
    """
    return candles[0]


def crossed_above(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close <= level < last_close


def crossed_below(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close >= level > last_close


def near(value: float, target: float, tolerance_points: float) -> bool:
    return abs(value - target) <= tolerance_points


# ==========
# Core Logic
# ==========
def analyze_signal(symbol: str, low_tf: str) -> Dict[str, Any]:
    """
    1) อ่าน H1/H4 → เอา High/Low ของแท่งที่ปิดล่าสุด เป็นกรอบ
       resistance = max(H1.high, H4.high)
       support    = min(H1.low,  H4.low)

    2) อ่าน low_tf (M5/M15) 2–4 แท่งล่าสุด เช็ค Breakout ด้วย close cross

    3) ถ้าเบรก → สร้างสัญญาณ พร้อม entry/sl/tp
       - entry: ที่เส้นกรอบ
       - sl: 250 จุด ฝั่งตรงข้าม
       - tp1: 500 จุด, tp2: 1000 จุด

    4) ถ้าเพิ่งเบรกไปแล้วและราคาย่อลง/เด้งกลับมา “ใกล้” เส้น (ระยะ ~50% ของแท่งเบรก
       หรือไม่เกิน 100 จุด) → ENTRY_READY
    """

    # --- High TF data ---
    h1 = previous_closed(fetch_series(symbol, "H1", 50))
    h4 = previous_closed(fetch_series(symbol, "H4", 50))

    resistance = max(h1.high, h4.high)
    support = min(h1.low, h4.low)

    # --- Low TF data (M5/M15) ---
    bars = fetch_series(symbol, low_tf, 30)
    if len(bars) < 3:
        return {
            "status": "ERROR",
            "message": "Not enough low timeframe data.",
        }

    last = bars[0]
    prev = bars[1]

    # Determine breakout
    up_break = crossed_above(prev.close, last.close, resistance)
    dn_break = crossed_below(prev.close, last.close, support)

    # Default outputs
    sl_points = 250.0
    tp1_points = 500.0
    tp2_points = 1000.0

    result: Dict[str, Any] = {
        "status": "OK",
        "symbol": symbol,
        "tf": low_tf,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "overlay": {
            "h1": h1.model_dump(),
            "h4": h4.model_dump(),
            "last": last.model_dump(),
            "prev": prev.model_dump(),
        },
        "signal": None,
        "entry": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "message": "",
    }

    # --- Breakout up ---
    if up_break:
        # breakout candle body height
        body = abs(last.close - prev.close)
        # จุดเข้า = ที่เส้นกรอบ (รีเทส)
        entry = resistance
        # ถ้าราคาย่อลงมาใกล้กรอบ (<= max(100, 0.5*body)) → ENTRY_READY
        tol = max(100.0, 0.5 * body)
        if near(last.close, entry, tol):
            result["signal"] = "ENTRY_READY_LONG"
            result["message"] = "Price retesting resistance after breakout."
        else:
            result["signal"] = "BREAKOUT_LONG"
            result["message"] = "Breakout above resistance. Wait pullback to enter."

        result["entry"] = round(entry, 2)
        result["sl"] = round(entry - sl_points, 2)
        result["tp1"] = round(entry + tp1_points, 2)
        result["tp2"] = round(entry + tp2_points, 2)
        return result

    # --- Breakout down ---
    if dn_break:
        body = abs(last.close - prev.close)
        entry = support
        tol = max(100.0, 0.5 * body)
        if near(last.close, entry, tol):
            result["signal"] = "ENTRY_READY_SHORT"
            result["message"] = "Price retesting support after breakout."
        else:
            result["signal"] = "BREAKOUT_SHORT"
            result["message"] = "Breakdown below support. Wait pullback to enter."

        result["entry"] = round(entry, 2)
        result["sl"] = round(entry + sl_points, 2)
        result["tp1"] = round(entry - tp1_points, 2)
        result["tp2"] = round(entry - tp2_points, 2)
        return result

    # --- No breakout yet ---
    # ใส่บอกกรอบเพื่อให้ไปเฝ้ารอได้
    result["signal"] = None
    result["message"] = "WAIT — รอราคาเบรกกรอบบน/ล่าง (ภายใน 2–3 ชม. ที่ TF ต่ำ)."
    return result


# ==========
# Routes
# ==========
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
