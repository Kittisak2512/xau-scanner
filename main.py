# main.py
import os
from typing import List, Dict, Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-10.3"

# ==========
# Config
# ==========
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
if not TD_API_KEY:
    # ให้ deploy ได้ แต่จะ error ตอนเรียก /signal
    pass

_allowed = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _allowed in ("", "*"):
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _allowed.split(",") if o.strip()]

# ==========
# App
# ==========
app = FastAPI(title="xau-scanner", version=APP_VERSION)

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
    tf_high: str = Field(..., description="H1 or H4", examples=["H1", "H4"])
    tf_low: str = Field(..., description="M5 or M15", examples=["M5", "M15"])

    @field_validator("tf_high")
    @classmethod
    def _vh(cls, v: str) -> str:
        v = v.upper()
        if v not in {"H1", "H4"}:
            raise ValueError("tf_high must be H1 or H4")
        return v

    @field_validator("tf_low")
    @classmethod
    def _vl(cls, v: str) -> str:
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


# ==========
# Utilities
# ==========
def td_interval(tf: str) -> str:
    tf = tf.upper()
    m = {"M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h"}
    if tf not in m:
        raise ValueError(f"Unsupported TF: {tf}")
    return m[tf]


def fetch_series(symbol: str, tf: str, size: int) -> List[Candle]:
    """ดึงแท่งจาก TwelveData (ล่าสุดมาก่อน)"""
    if not TD_API_KEY:
        raise HTTPException(500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",
        "timezone": "UTC",
        "apikey": TD_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()
    except Exception:
        raise HTTPException(502, detail="Upstream error")

    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(502, detail=str(data.get("message", "API error")))
    values = data.get("values")
    if not values:
        raise HTTPException(502, detail="No data")

    out: List[Candle] = []
    for v in values:
        try:
            out.append(
                Candle(
                    dt=v["datetime"],
                    open=float(v["open"]),
                    high=float(v["high"]),
                    low=float(v["low"]),
                    close=float(v["close"]),
                )
            )
        except Exception:
            continue
    if not out:
        raise HTTPException(502, detail="Parse candles failed")
    return out  # latest first


def previous_closed(bars: List[Candle]) -> Candle:
    """TwelveData ส่งแบบปิดล่าสุดอยู่หน้า (desc)"""
    return bars[0]


def close_cross_up(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close <= level < last_close


def close_cross_dn(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close >= level > last_close


def wick_cross_up(prev_high: float, last_high: float, level: float) -> bool:
    """ไส้เทียนทะลุขึ้น"""
    # ให้ last high > level และก่อนหน้าไม่ทะลุชัด ๆ
    return last_high > level and prev_high <= level


def wick_cross_dn(prev_low: float, last_low: float, level: float) -> bool:
    """ไส้เทียนทะลุลง"""
    return last_low < level and prev_low >= level


def near(v: float, target: float, tol: float) -> bool:
    return abs(v - target) <= tol


# ==========
# Core
# ==========
def analyze_breakout(symbol: str, tf_high: str, tf_low: str) -> Dict[str, Any]:
    """
    กรอบ = high/low ของแท่งปิดล่าสุด ใน TF สูง (H1/H4)
    เบรก = (A) close cross หรือ (B) wick swing (ไส้) ที่ TF ต่ำ (M5/M15)
    Pullback entry = ราคาเข้าใกล้เส้น <= max(100, 0.5*bar_range)
    """
    # --- กรอบ TF สูง ---
    hi_bar = previous_closed(fetch_series(symbol, tf_high, 5))
    upper = hi_bar.high
    lower = hi_bar.low

    # --- TF ต่ำ ---
    # ดึง 60 แท่งพอ (M5 ~ 5 ชม., M15 ~ 15 ชม.) เพื่อให้มีบริบท
    low_bars = fetch_series(symbol, tf_low, 60)
    if len(low_bars) < 3:
        return {"status": "ERROR", "message": "Not enough low timeframe data."}

    last = low_bars[0]
    prev = low_bars[1]

    # กฎเบรก
    up_break = close_cross_up(prev.close, last.close, upper) or wick_cross_up(prev.high, last.high, upper)
    dn_break = close_cross_dn(prev.close, last.close, lower) or wick_cross_dn(prev.low, last.low, lower)

    # tolerance สำหรับ pullback
    bar_range = max(1.0, last.high - last.low)
    tol = max(100.0, 0.5 * bar_range)

    # ค่าคงที่ SL/TP
    SL_PTS = 250.0
    TP1_PTS = 500.0
    TP2_PTS = 1000.0

    result: Dict[str, Any] = {
        "status": "OK",
        "symbol": symbol,
        "tf_high": tf_high,
        "tf_low": tf_low,
        "box": {
            "upper": round(upper, 2),
            "lower": round(lower, 2),
            "ref_bar": hi_bar.model_dump(),
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
        "message": "WAIT — รอราคาเบรกกรอบบน/ล่าง (ที่ TF ต่ำ).",
    }

    # --- เบรกขึ้น ---
    if up_break:
        entry = upper
        entry_50 = round(entry + 0.5 * min(TP1_PTS, max(0.0, entry - last.low)), 2)  # จุดเผื่อ half move ขึ้น
        if near(last.close, entry, tol):
            result.update(
                dict(
                    signal="ENTRY_READY_LONG",
                    message="Retest ใกล้เส้นกรอบบนหลัง Breakout.",
                    entry=round(entry, 2),
                    entry_50=entry_50,
                    sl=round(entry - SL_PTS, 2),
                    tp1=round(entry + TP1_PTS, 2),
                    tp2=round(entry + TP2_PTS, 2),
                )
            )
        else:
            result.update(
                dict(
                    signal="BREAKOUT_LONG",
                    message="ปิด/ไส้ทะลุกรอบบน รอราคาย่อกลับทดสอบเส้นเพื่อเข้าซื้อ.",
                    entry=round(entry, 2),
                    entry_50=entry_50,
                    sl=round(entry - SL_PTS, 2),
                    tp1=round(entry + TP1_PTS, 2),
                    tp2=round(entry + TP2_PTS, 2),
                )
            )
        return result

    # --- เบรกลง ---
    if dn_break:
        entry = lower
        entry_50 = round(entry - 0.5 * min(TP1_PTS, max(0.0, last.high - entry)), 2)
        if near(last.close, entry, tol):
            result.update(
                dict(
                    signal="ENTRY_READY_SHORT",
                    message="Retest ใกล้เส้นกรอบล่างหลัง Breakdown.",
                    entry=round(entry, 2),
                    entry_50=entry_50,
                    sl=round(entry + SL_PTS, 2),
                    tp1=round(entry - TP1_PTS, 2),
                    tp2=round(entry - TP2_PTS, 2),
                )
            )
        else:
            result.update(
                dict(
                    signal="BREAKOUT_SHORT",
                    message="ปิด/ไส้ทะลุกรอบล่าง รอราคาเด้งกลับทดสอบเส้นเพื่อเข้าขาย.",
                    entry=round(entry, 2),
                    entry_50=entry_50,
                    sl=round(entry + SL_PTS, 2),
                    tp1=round(entry - TP1_PTS, 2),
                    tp2=round(entry - TP2_PTS, 2),
                )
            )
        return result

    # ยังไม่เบรก
    return result


# ==========
# Routes
# ==========
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


@app.post("/signal")
def signal(req: SignalRequest):
    try:
        return analyze_breakout(req.symbol, req.tf_high, req.tf_low)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
