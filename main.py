# main.py
import os
from datetime import datetime, timezone
from typing import List, Dict, Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-10.2"

# =========================
# Environment Config
# =========================
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _ALLOWED in ("", "*"):
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# Points (ปรับได้ผ่าน ENV หากต้องการ)
SL_POINTS = float(os.getenv("SL_POINTS", "250"))
TP1_POINTS = float(os.getenv("TP1_POINTS", "500"))
TP2_POINTS = float(os.getenv("TP2_POINTS", "1000"))

# =========================
# FastAPI
# =========================
app = FastAPI(title="xau-scanner", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Models
# =========================
class SignalRequest(BaseModel):
    symbol: str = Field(..., examples=["XAU/USD"])
    tf_high: str = Field(..., description="High timeframe (H1 or H4)", examples=["H1", "H4"])
    tf_low: str = Field(..., description="Low timeframe (M5 or M15)", examples=["M5", "M15"])

    @field_validator("tf_high")
    @classmethod
    def _chk_high(cls, v: str) -> str:
        v = v.upper()
        if v not in {"H1", "H4"}:
            raise ValueError("tf_high must be H1 or H4")
        return v

    @field_validator("tf_low")
    @classmethod
    def _chk_low(cls, v: str) -> str:
        v = v.upper()
        if v not in {"M5", "M15"}:
            raise ValueError("tf_low must be M5 or M15")
        return v


class Candle(BaseModel):
    dt: str
    open: float
    high: float
    low: float
    close: float


# =========================
# Utilities
# =========================
def td_interval(tf: str) -> str:
    """Map timeframe to TwelveData interval string."""
    tf = tf.upper()
    mapping = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
    }
    if tf not in mapping:
        raise ValueError(f"Unsupported TF: {tf}")
    return mapping[tf]


def fetch_series(symbol: str, tf: str, size: int = 120) -> List[Candle]:
    """Fetch time series (latest first) from TwelveData."""
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",          # latest first
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
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
                    dt=str(v["datetime"]),
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
    """TwelveData (order=desc) index 0 is latest closed bar."""
    return candles[0]


def crossed_above(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close <= level < last_close


def crossed_below(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close >= level > last_close


def near(value: float, target: float, tolerance_points: float) -> bool:
    return abs(value - target) <= tolerance_points


# =========================
# Core Logic
# =========================
def analyze_breakout(symbol: str, tf_high: str, tf_low: str) -> Dict[str, Any]:
    """
    กติกา:
      1) เอาแท่งปิดล่าสุดของ TF สูง (H1/H4) มาตีกรอบ: upper=high, lower=low
      2) อ่าน TF ต่ำ (M5/M15) ล่าสุด 30 แท่ง → เช็ค breakout ด้วย close cross
      3) หากเพิ่งเบรก:
           - signal = BREAKOUT_LONG/SHORT
           - ถ้าราคารีเทสใกล้เส้น (<= max(100, 50% body แท่งเบรก)) → ENTRY_READY_*
           - entry = เส้นกรอบ, sl/tp = ตามค่าจุดที่กำหนด
      4) ไม่เบรก → WAIT
    """
    # --- High TF box ---
    high_bars = fetch_series(symbol, tf_high, size=10)
    high_ref = previous_closed(high_bars)  # ใช้แท่งปิดล่าสุดของ TF สูง
    upper = float(high_ref.high)
    lower = float(high_ref.low)

    # --- Low TF bars ---
    low_bars = fetch_series(symbol, tf_low, size=30)
    if len(low_bars) < 2:
        return {"status": "ERROR", "message": "Not enough low timeframe data."}

    last = low_bars[0]
    prev = low_bars[1]

    up_break = crossed_above(prev.close, last.close, upper)
    dn_break = crossed_below(prev.close, last.close, lower)

    # โครงผลลัพธ์พื้นฐาน
    result: Dict[str, Any] = {
        "status": "OK",
        "symbol": symbol,
        "tf_high": tf_high,
        "tf_low": tf_low,
        "box": {
            "upper": round(upper, 2),
            "lower": round(lower, 2),
            "ref_bar": {
                "dt": high_ref.dt,
                "open": high_ref.open,
                "high": high_ref.high,
                "low": high_ref.low,
                "close": high_ref.close,
            },
        },
        "overlay": {
            "last": last.model_dump(),
            "prev": prev.model_dump(),
        },
        "signal": None,
        "entry": None,
        "entry_50": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "message": "",
    }

    # tolerance สำหรับรีเทส
    body = abs(last.close - prev.close)
    retest_tol = max(100.0, 0.5 * body)

    # ===== Breakout ขึ้น =====
    if up_break:
        entry = upper
        result["entry"] = round(entry, 2)
        result["entry_50"] = round(entry - 0.5 * (entry - prev.close), 2)  # เข้าเพิ่มเผื่อย่อ 50%
        result["sl"] = round(entry - SL_POINTS, 2)
        result["tp1"] = round(entry + TP1_POINTS, 2)
        result["tp2"] = round(entry + TP2_POINTS, 2)

        if near(last.close, entry, retest_tol):
            result["signal"] = "ENTRY_READY_LONG"
            result["message"] = "Retest ใกล้เส้นกรอบบนหลังเบรก — พร้อมเข้า Long."
        else:
            result["signal"] = "BREAKOUT_LONG"
            result["message"] = "เบรกกรอบบนแล้ว — รอย่อกลับมาใกล้เส้นกรอบค่อยเข้า Long."
        return result

    # ===== Breakout ลง =====
    if dn_break:
        entry = lower
        result["entry"] = round(entry, 2)
        result["entry_50"] = round(entry + 0.5 * (prev.close - entry), 2)  # เด้ง 50% ค่อยยิงต่อ
        result["sl"] = round(entry + SL_POINTS, 2)
        result["tp1"] = round(entry - TP1_POINTS, 2)
        result["tp2"] = round(entry - TP2_POINTS, 2)

        if near(last.close, entry, retest_tol):
            result["signal"] = "ENTRY_READY_SHORT"
            result["message"] = "Retest ใกล้เส้นกรอบล่างหลังเบรก — พร้อมเข้า Short."
        else:
            result["signal"] = "BREAKOUT_SHORT"
            result["message"] = "เบรกกรอบล่างแล้ว — รอเด้งกลับมาใกล้เส้นกรอบค่อยเข้า Short."
        return result

    # ===== ยังไม่เบรก =====
    result["message"] = "WAIT — รอราคาเบรกกรอบบน/ล่าง (ที่ TF ต่ำ)."
    return result


# =========================
# Routes
# =========================
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/signal")
def signal(req: SignalRequest):
    try:
        return analyze_breakout(req.symbol, req.tf_high, req.tf_low)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
