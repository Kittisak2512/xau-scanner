import os
import math
from typing import Optional, Literal, Dict, Any
from datetime import datetime, timedelta, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_NAME = "xau-scanner"
APP_VERSION = "2025-09-09.1"

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")

app = FastAPI(title="XAU Scanner", version=APP_VERSION)

# ---- CORS ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGINS] if ALLOWED_ORIGINS and ALLOWED_ORIGINS != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Timeframe map for TwelveData ----
TF_MAP = {
    "M5": "5min",
    "M15": "15min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1day",
}

# -------- Models --------
class SignalRequest(BaseModel):
    symbol: str = Field(..., examples=["XAU/USD", "XAUUSD"])
    higher_tf: Literal["H1", "H4", "D1"]
    lower_tf: Literal["M5", "M15"]
    sl_points: int = 250
    tp1_points: int = 500
    tp2_points: int = 1000

    # Optional: ใช้กรอบเอง (จะไม่ไปเรียก TwelveData)
    box_high: Optional[float] = None
    box_low: Optional[float] = None


# -------- Utilities --------
def norm_symbol(sym: str) -> str:
    # TwelveData ใช้ "XAU/USD" แนะนำให้ใช้สตริงนี้
    s = sym.upper().replace("XAUUSD", "XAU/USD").replace("XAU XUSD", "XAU/USD")
    return s

def td_url(path: str, params: Dict[str, Any]) -> str:
    base = "https://api.twelvedata.com"
    q = "&".join([f"{k}={requests.utils.quote(str(v))}" for k, v in params.items()])
    return f"{base}{path}?{q}"

def fetch_candles(symbol: str, interval: str, outputsize: int = 60) -> Dict[str, Any]:
    """ดึง OHLC ล่าสุดจาก TwelveData (คืน dict: meta, values(list))"""
    if not TWELVEDATA_API_KEY:
        raise HTTPException(500, detail="TWELVEDATA_API_KEY not set")

    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    url = td_url("/time_series", params)
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        raise HTTPException(502, detail=f"TwelveData {r.status_code}")
    data = r.json()
    if "status" in data and data.get("status") == "error":
        raise HTTPException(502, detail=f"TwelveData error: {data.get('message')}")
    if "values" not in data or not data["values"]:
        raise HTTPException(502, detail="TwelveData no data")
    return data

def latest_close(data: Dict[str, Any]) -> float:
    # values[0] คือแท่งล่าสุด (close เป็น string)
    try:
        return float(data["values"][0]["close"])
    except Exception:
        raise HTTPException(500, detail="Malformed TwelveData response")

def recent_high_low(data: Dict[str, Any], bars: int = 20) -> (float, float):
    """คำนวนกรอบ: high สูงสุดและ low ต่ำสุด ของช่วงล่าสุด bars แท่ง (TF สูง)"""
    vals = data["values"][:bars]
    highs = [float(v["high"]) for v in vals]
    lows = [float(v["low"]) for v in vals]
    return max(highs), min(lows)

def calc_entry_setups(side: Literal["LONG","SHORT"], entry: float, sl_pts: int, tp1_pts: int, tp2_pts: int):
    if side == "LONG":
        sl = entry - sl_pts
        tp1 = entry + tp1_pts
        tp2 = entry + tp2_pts
    else:
        sl = entry + sl_pts
        tp1 = entry - tp1_pts
        tp2 = entry - tp2_pts
    return sl, tp1, tp2


# -------- Routes --------
@app.get("/")
def root():
    return {"app": APP_NAME, "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

@app.post("/signal")
def signal(req: SignalRequest):
    symbol = norm_symbol(req.symbol)
    hi_tf = req.higher_tf
    lo_tf = req.lower_tf

    # 1) กำหนดกรอบ (box) : ใช้ค่าที่ผู้ใช้ส่งมา หรือคำนวนจาก TwelveData
    if req.box_high is not None and req.box_low is not None:
        box_high, box_low = float(req.box_high), float(req.box_low)
        box_source = "manual"
    else:
        # ใช้ TwelveData หา high/low จาก TF สูง (ช่วง N แท่งหลังสุด)
        if hi_tf not in TF_MAP:
            raise HTTPException(400, detail="Invalid higher_tf")
        hi_data = fetch_candles(symbol, TF_MAP[hi_tf], outputsize=120)
        # กรอบจาก 20 แท่งหลังสุด (ปรับได้)
        box_high, box_low = recent_high_low(hi_data, bars=20)
        box_source = "twelvedata"

    if lo_tf not in TF_MAP:
        raise HTTPException(400, detail="Invalid lower_tf")
    lo_data = fetch_candles(symbol, TF_MAP[lo_tf], outputsize=5)
    lo_close = latest_close(lo_data)

    # 2) Logic: Break + Close (เข้าเมื่อ "แท่งปิด" ทะลุกกรอบทันที)
    status = "WATCH"
    side: Optional[Literal["LONG","SHORT"]] = None
    reason = "ยังไม่ Breakout"
    entry: Optional[float] = None

    if lo_close > box_high:
        status = "ENTRY"
        side = "LONG"
        reason = f"Break+Close above {hi_tf} box."
        entry = lo_close
    elif lo_close < box_low:
        status = "ENTRY"
        side = "SHORT"
        reason = f"Break+Close below {hi_tf} box."
        entry = lo_close

    # 3) สร้าง SL/TP (เฉพาะกรณี ENTRY)
    sl = tp1 = tp2 = None
    if status == "ENTRY" and side and entry is not None:
        sl, tp1, tp2 = calc_entry_setups(side, entry, req.sl_points, req.tp1_points, req.tp2_points)

    return {
        "status": status,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "reason": reason,
        "box": {"high": box_high, "low": box_low, "tf": hi_tf, "source": box_source},
        "lower_tf_close": lo_close,
        "params": {"sl_points": req.sl_points, "tp1_points": req.tp1_points, "tp2_points": req.tp2_points},
        "meta": {"symbol": symbol, "higher_tf": hi_tf, "lower_tf": lo_tf, "version": APP_VERSION},
    }
