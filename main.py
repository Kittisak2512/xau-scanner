import os
import math
from typing import Optional, Literal, List, Dict, Any
from datetime import datetime, timedelta, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_NAME = "xau-scanner"
APP_VERSION = datetime.now(timezone.utc).strftime("%Y-%m-%d.%H%M")

# -------- Config from ENV --------
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
if not TD_API_KEY:
    # ให้รันได้แม้ยังไม่ใส่คีย์ แต่จะ error ตอนยิง /signal
    pass

# CORS
_allowed_env = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _allowed_env == "" or _allowed_env == "*":
    ALLOWED_ORIGINS: List[str] = ["*"]
else:
    ALLOWED_ORIGINS = [o.strip() for o in _allowed_env.split(",") if o.strip()]

# ----------- FastAPI -------------
app = FastAPI(title="XAU Scanner — M5/M15 Signals", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Models ---------------
TFType = Literal["M5", "M15"]

class SignalRequest(BaseModel):
    symbol: str = Field(default="XAU/USD", description="เช่น XAU/USD")
    tf: TFType = Field(default="M5", description="M5 หรือ M15")

    # เผื่อ front-end เดิมยังยิงมา (ไม่ใช้ในการคำนวณ แต่วางไว้กันพัง)
    sl_points: Optional[float] = None
    tp1_points: Optional[float] = None
    tp2_points: Optional[float] = None


class SignalResponse(BaseModel):
    status: Literal["OK", "WAIT", "ERROR"]
    symbol: str
    tf: TFType
    signal: Optional[Literal["BUY", "SELL"]] = None
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    box_window: str
    message: str
    overlay: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


# -------- Utilities --------------
def td_interval(tf: TFType) -> str:
    return "5min" if tf == "M5" else "15min"

def lookback_count(tf: TFType) -> int:
    # 2–3 ชั่วโมง: M5 ~ 36, M15 ~ 12
    return 36 if tf == "M5" else 12

def round_price(x: float) -> float:
    # ทองคำโบรกส่วนมาก 2 จุดทศนิยมพอ
    return round(float(x), 2)

def fetch_candles(symbol: str, tf: TFType, api_key: str) -> List[Dict[str, Any]]:
    """
    ดึง OHLC จาก TwelveData (ล่าสุดมาก่อน) แล้ว reverse ให้เก่าสุด -> ใหม่สุด
    โครงสร้างคาดหวัง: {"values":[{"datetime": "...","open":"..","high":"..","low":"..","close":".."}, ...]}
    """
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": max(lookback_count(tf) + 5, 40),  # +เผื่อขาด
        "format": "JSON",
        "apikey": api_key,
    }
    try:
        r = requests.get(url, params=params, timeout=12)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"TwelveData request error: {e}")

    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="TwelveData returned non-JSON")

    if "values" not in data:
        # ส่งดิบกลับให้ดีบักได้ง่าย
        raise HTTPException(status_code=502, detail=f"TwelveData bad payload: {data}")

    vals = data["values"]
    if not isinstance(vals, list) or len(vals) < 5:
        raise HTTPException(status_code=502, detail="Not enough candles from TwelveData")

    # ล่าสุดมาก่อน -> reverse
    vals = list(reversed(vals))
    # แปลง numeric
    cleaned = []
    for v in vals:
        try:
            cleaned.append({
                "dt": v["datetime"],
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
            })
        except Exception:
            # ข้ามแถวที่แปลงไม่ได้
            continue
    return cleaned


def compute_levels(candles: List[Dict[str, Any]], tf: TFType) -> Dict[str, float]:
    """
    ใช้ lookback ~2–3 ชม. 'ก่อนหน้า' แท่งปิดล่าสุด เพื่อหาแนวรับ/แนวต้าน
    - support = min(low) ในช่วง lookback
    - resistance = max(high) ในช่วง lookback
    """
    n = lookback_count(tf)
    if len(candles) < n + 2:
        # เผื่อข้อมูลน้อย
        n = min(len(candles) - 2, n)

    # ปิดล่าสุดใช้แท่ง [-1], "last_closed" = แท่งก่อนล่าสุด [-2] (กันหลุดระหว่างแท่ง)
    last_closed = candles[-2]
    window = candles[-(n+2):-2]  # exclude last two bars

    sup = min(c["low"] for c in window)
    res = max(c["high"] for c in window)

    return {
        "support": round_price(sup),
        "resistance": round_price(res),
        "last_closed": last_closed,  # ส่งกลับใช้ตัดสิน
        "count": len(window),
    }


def make_signal(last: Dict[str, Any], support: float, resistance: float) -> Dict[str, Any]:
    """
    เงื่อนไขซิกแนล:
    - BUY  เมื่อ close > resistance และ open <= resistance (ทะลุ + ปิดเหนือโซน)
    - SELL เมื่อ close < support    และ open >= support    (ทะลุลง + ปิดใต้โซน)
    """
    o = last["open"]
    h = last["high"]
    l = last["low"]
    c = last["close"]

    if (c > resistance) and (o <= resistance):
        side = "BUY"
        entry = c
        sl = l
        risk = abs(entry - sl)
        if risk <= 0:
            return {"signal": None}
        tp1 = entry + risk
        tp2 = entry + 2 * risk
        return {
            "signal": side,
            "entry": round_price(entry),
            "sl": round_price(sl),
            "tp1": round_price(tp1),
            "tp2": round_price(tp2),
        }

    if (c < support) and (o >= support):
        side = "SELL"
        entry = c
        sl = h
        risk = abs(entry - sl)
        if risk <= 0:
            return {"signal": None}
        tp1 = entry - risk
        tp2 = entry - 2 * risk
        return {
            "signal": side,
            "entry": round_price(entry),
            "sl": round_price(sl),
            "tp1": round_price(tp1),
            "tp2": round_price(tp2),
        }

    return {"signal": None}


def overlay_text(sig: str, entry: float, sl: float, tp1: float, tp2: float) -> str:
    side = "LONG" if sig == "BUY" else "SHORT"
    return f"ENTRY {side} @ {entry:.2f} | SL {sl:.2f} | TP1 {tp1:.2f} | TP2 {tp2:.2f}"


# ------------- Routes -------------
@app.get("/")
def root():
    return {"app": APP_NAME, "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"status": "healthy", "time": datetime.now(timezone.utc).isoformat()}

@app.post("/signal", response_model=SignalResponse)
def get_signal(req: SignalRequest):
    if not TD_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")

    # 1) ดึงข้อมูลจาก TwelveData
    candles = fetch_candles(req.symbol, req.tf, TD_API_KEY)

    # 2) หาแนวรับต้านจากหน้าต่าง 2–3 ชั่วโมง (ยกเว้นแท่งล่าสุด)
    levels = compute_levels(candles, req.tf)
    support = levels["support"]
    resistance = levels["resistance"]
    last = levels["last_closed"]

    # 3) ตัดสินซิกแนลจากแท่งปิดล่าสุด
    sig = make_signal(last, support, resistance)

    box_desc = f"lookback={levels['count']} bars (~2–3h @ {req.tf})"
    if sig["signal"] is None:
        return SignalResponse(
            status="WAIT",
            symbol=req.symbol,
            tf=req.tf,
            signal=None,
            support=support,
            resistance=resistance,
            box_window=box_desc,
            message="WAIT — ราคายังไม่ปิดทะลุโซนแนวรับ/ต้าน ภายใน 2–3 ชม.",
            raw={"last_closed": last},
        )

    # 4) จัดรูปซิกแนลพร้อม overlay
    text = overlay_text(sig["signal"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"])

    return SignalResponse(
        status="OK",
        symbol=req.symbol,
        tf=req.tf,
        signal=sig["signal"],
        entry=sig["entry"],
        sl=sig["sl"],
        tp1=sig["tp1"],
        tp2=sig["tp2"],
        support=support,
        resistance=resistance,
        box_window=box_desc,
        message="Signal generated by Break+Close rule.",
        overlay=text,
        raw={"last_closed": last},
    )
