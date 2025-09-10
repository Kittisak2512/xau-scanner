import os
from typing import List, Dict, Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-10.2"

# ==========
# Config
# ==========
TD_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
if not TD_KEY:
    # ให้ deploy ได้ แต่จะ error เมื่อเรียก /signal
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
# Helpers
# ==========
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
    if not TD_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",     # latest first
        "timezone": "UTC",
        "apikey": TD_KEY,
    }
    r = requests.get(url, params=params, timeout=25)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON")

    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(status_code=502, detail=str(data.get("message", "API error")))
    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData")

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
        raise HTTPException(status_code=502, detail="Cannot parse candles")
    return out  # latest first


def prev_closed(candles: List[Candle]) -> Candle:
    # TwelveData ส่งแท่งที่ปิดล่าสุดมาเป็นอันดับแรกเมื่อ order=desc
    return candles[0]


def near(val: float, target: float, tol: float) -> bool:
    return abs(val - target) <= tol


# ==========
# Core
# ==========
def analyze_breakout(symbol: str, tf_high: str, tf_low: str) -> Dict[str, Any]:
    """
    กล่องกรอบ = high/low ของแท่งปิดล่าสุดจาก TF สูง (H1/H4)
    การเบรก = ใช้ 'สวิง' ของแท่ง TF ต่ำ (M5/M15)
              ถ้า last.high > upper → breakout ขึ้น
                 last.low  < lower → breakout ลง
    สัญญาณ ENTRY_READY เมื่อรีเทสต์ใกล้กรอบ (<= max(100 จุด, 0.5×body_break))
    """
    # --- High TF: สร้างกรอบ ---
    high_bars = fetch_series(symbol, tf_high, 10)
    hb = prev_closed(high_bars)
    upper = hb.high
    lower = hb.low

    # --- Low TF: ตรวจเบรกโดยสวิง high/low ---
    low_bars = fetch_series(symbol, tf_low, 40)
    if len(low_bars) < 2:
        return {"status": "ERROR", "message": "Not enough low TF data."}

    last = low_bars[0]
    prevb = low_bars[1]

    # ขนาดแท่งเบรก (ใช้แท่งล่าสุดเป็นเบรกถ้าเกิด)
    body_break = abs(last.close - last.open)
    tol_retest = max(100.0, 0.5 * body_break)

    # ค่า config จุด
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
            "ref_bar": hb.model_dump(),
        },
        "overlay": {
            "last": last.model_dump(),
            "prev": prevb.model_dump(),
        },
        "signal": None,
        "entry": None,
        "entry_50": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "message": "",
    }

    # --------- Breakout by swing high/low ----------
    if last.high > upper:
        entry = upper
        result["entry"] = round(entry, 2)
        # จุด 50% ของแท่งเบรกจากกรอบขึ้นไป
        entry_50 = entry + 0.5 * (last.close - entry)
        result["entry_50"] = round(entry_50, 2)

        if near(last.close, entry, tol_retest):
            result["signal"] = "ENTRY_READY_LONG"
            result["message"] = "Retest resistance after swing breakout."
        else:
            result["signal"] = "BREAKOUT_LONG"
            result["message"] = "Swing breakout above resistance. Wait pullback."

        result["sl"] = round(entry - SL_PTS, 2)
        result["tp1"] = round(entry + TP1_PTS, 2)
        result["tp2"] = round(entry + TP2_PTS, 2)
        return result

    if last.low < lower:
        entry = lower
        result["entry"] = round(entry, 2)
        entry_50 = entry - 0.5 * (entry - last.close)
        result["entry_50"] = round(entry_50, 2)

        if near(last.close, entry, tol_retest):
            result["signal"] = "ENTRY_READY_SHORT"
            result["message"] = "Retest support after swing breakdown."
        else:
            result["signal"] = "BREAKOUT_SHORT"
            result["message"] = "Swing breakdown below support. Wait pullback."

        result["sl"] = round(entry + SL_PTS, 2)
        result["tp1"] = round(entry - TP1_PTS, 2)
        result["tp2"] = round(entry - TP2_PTS, 2)
        return result

    # ยังไม่เบรก
    result["message"] = "WAIT — รอราคาเบรกกรอบบน/ล่าง (ที่ TF ต่ำ)."
    return result


# ==========
# Routes
# ==========
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/signal")
def signal(req: SignalRequest):
    try:
        return analyze_breakout(req.symbol, req.tf_high, req.tf_low)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
