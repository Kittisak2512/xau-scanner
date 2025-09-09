# main.py
from __future__ import annotations

import os
import math
import httpx
from typing import Optional, Literal, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

# ---------------------------------------------------------------------
# Config & helpers
# ---------------------------------------------------------------------

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
if not TWELVEDATA_API_KEY:
    # ไม่หยุดแอป แต่จะแจ้งเตือนเวลาถูกเรียกใช้งาน
    print("[WARN] TWELVEDATA_API_KEY is not set. /signal will fail without it.")

# แปลงชื่อ TF ที่ UI ใช้ -> interval ของ TwelveData
INTERVAL_MAP = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H2": "2h",
    "H4": "4h",
    "D1": "1day",
}

def tf_to_interval(tf: str) -> str:
    tf = tf.upper()
    if tf not in INTERVAL_MAP:
        raise HTTPException(status_code=400, detail=f"Unsupported TF: {tf}")
    return INTERVAL_MAP[tf]

def as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

# ---------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------

app = FastAPI(title="XAU Scanner", version="2025-09-07.2")

# CORS
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
if _allowed_origins_env:
    # คั่นด้วย , หรือ ช่องว่าง ก็ได้
    _origins = [o.strip() for part in _allowed_origins_env.split(",") for o in part.split()]
else:
    _origins = ["*"]  # เปิดกว้างถ้ายังไม่ตั้งค่า

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------

class SignalRequest(BaseModel):
    symbol: str = Field(..., example="XAU/USD")
    higher_tf: str = Field(..., example="H4")
    lower_tf: str = Field(..., example="M15")
    sl_points: int = Field(250, ge=1)
    tp1_points: int = Field(500, ge=1)
    tp2_points: int = Field(1000, ge=1)

    @validator("symbol")
    def v_symbol(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("symbol is empty")
        return v

class RefBox(BaseModel):
    higher_tf: str
    lower_tf: str
    H_high: float
    H_low: float
    last: float
    box_height: float

class SignalResponse(BaseModel):
    status: Literal["OK", "WATCH", "ENTRY", "ERROR"]
    signal: Literal["NONE", "LONG", "SHORT"]
    reason: str
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    ref: Optional[RefBox] = None
    backend: str = "xau-scanner"
    version: str = app.version

# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

@app.get("/")
def root():
    return {"app": "xau-scanner", "version": app.version, "ok": True}

@app.get("/health")
def health():
    return {"status": "ok"}

# ---------------------------------------------------------------------
# TwelveData fetchers
# ---------------------------------------------------------------------

BASE_URL = "https://api.twelvedata.com"

async def fetch_latest_closed_candle(
    client: httpx.AsyncClient, symbol: str, interval: str
) -> Dict[str, Any]:
    """
    ดึงแท่งเทียน 'ปิดแล้ว' ล่าสุด 2 แท่ง และใช้แท่ง [-2] เป็น "ล่าสุดที่ปิดแล้วจริง"
    (กันกรณีแท่ง[-1] ยังวิ่งอยู่ในช่วงเวลาปัจจุบัน)
    """
    url = f"{BASE_URL}/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 2,       # พอสำหรับดึง -2 เป็นแท่งปิดล่าสุด
        "order": "desc",
        "apikey": TWELVEDATA_API_KEY,
    }
    r = await client.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    if "values" not in data or not isinstance(data["values"], list) or len(data["values"]) < 2:
        raise HTTPException(status_code=502, detail=f"Not enough data for {symbol} {interval}")

    # values เรียงจากใหม่ -> เก่า
    # [-1] = เก่าสุด, [0] = ยังวิ่ง, [1] = ปิดล่าสุด
    closed = data["values"][1]
    # normalize
    return {
        "datetime": closed.get("datetime"),
        "open": as_float(closed.get("open")),
        "high": as_float(closed.get("high")),
        "low": as_float(closed.get("low")),
        "close": as_float(closed.get("close")),
    }

async def fetch_last_close(
    client: httpx.AsyncClient, symbol: str, interval: str
) -> float:
    """
    close ล่าสุดที่ 'ปิดแล้ว' บน TF ต่ำ
    """
    c = await fetch_latest_closed_candle(client, symbol, interval)
    return as_float(c["close"])

# ---------------------------------------------------------------------
# Break + Close logic
# ---------------------------------------------------------------------

def compute_trade_levels(
    direction: Literal["LONG", "SHORT"],
    entry: float,
    sl_points: int,
    tp1_points: int,
    tp2_points: int,
) -> Dict[str, float]:
    if direction == "LONG":
        sl = entry - sl_points
        tp1 = entry + tp1_points
        tp2 = entry + tp2_points
    else:  # SHORT
        sl = entry + sl_points
        tp1 = entry - tp1_points
        tp2 = entry - tp2_points
    return {"sl": sl, "tp1": tp1, "tp2": tp2}

@app.post("/signal", response_model=SignalResponse)
async def signal(req: SignalRequest):
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="TWELVEDATA_API_KEY is not configured on server")

    higher_iv = tf_to_interval(req.higher_tf)
    lower_iv = tf_to_interval(req.lower_tf)

    # ดึงข้อมูล
    async with httpx.AsyncClient() as client:
        # กล่อง (กรอบ) ใช้แท่ง TF สูงที่ "ปิดล่าสุด"
        H = await fetch_latest_closed_candle(client, req.symbol, higher_iv)
        H_high = as_float(H["high"])
        H_low = as_float(H["low"])

        # ราคา close ล่าสุด (ปิดแล้ว) บน TF ต่ำ
        last_close = await fetch_last_close(client, req.symbol, lower_iv)

    box_height = max(H_high - H_low, 0.0)

    # ตรวจ Break + Close
    entry_dir: Literal["LONG", "SHORT"] = "LONG"
    decide = "WATCH"

    if last_close > H_high:
        decide = "ENTRY"
        entry_dir = "LONG"
    elif last_close < H_low:
        decide = "ENTRY"
        entry_dir = "SHORT"
    else:
        decide = "WATCH"

    if decide == "WATCH":
        return SignalResponse(
            status="WATCH",
            signal="NONE",
            reason=f"ยังไม่ Breakout โซน {req.higher_tf}/{req.lower_tf}",
            ref=RefBox(
                higher_tf=req.higher_tf,
                lower_tf=req.lower_tf,
                H_high=H_high,
                H_low=H_low,
                last=last_close,
                box_height=box_height,
            ),
        )

    # ENTRY
    levels = compute_trade_levels(
        direction=entry_dir,
        entry=last_close,
        sl_points=req.sl_points,
        tp1_points=req.tp1_points,
        tp2_points=req.tp2_points,
    )

    # ข้อความสรุป
    msg = (
        f"ENTRY {entry_dir} @ {last_close:.2f} | "
        f"SL {levels['sl']:.2f} | TP1 {levels['tp1']:.2f} | TP2 {levels['tp2']:.2f}"
    )

    return SignalResponse(
        status="ENTRY",
        signal=entry_dir,
        reason=msg,
        entry=last_close,
        sl=levels["sl"],
        tp1=levels["tp1"],
        tp2=levels["tp2"],
        ref=RefBox(
            higher_tf=req.higher_tf,
            lower_tf=req.lower_tf,
            H_high=H_high,
            H_low=H_low,
            last=last_close,
            box_height=box_height,
        ),
    )
