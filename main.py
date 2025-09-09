import os
import math
import time
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests

# ---------- Config ----------
APP_NAME = "xau-scanner"
APP_VERSION = time.strftime("%Y-%m-%d.%H%M%S")

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

# อนุญาต origin จาก env ชื่อ ALLOWED_ORIGINS (comma-separated) ถ้าไม่ตั้ง ให้เป็น *
_allowed = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _allowed and _allowed != "*":
    ORIGINS = [o.strip() for o in _allowed.split(",") if o.strip()]
else:
    ORIGINS = ["*"]

# ---------- App ----------
app = FastAPI(title=APP_NAME, version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Models ----------
class ScanReq(BaseModel):
    symbol: str = "XAU/USD"
    higher_tf: str = "H4"      # H1/H4
    lower_tf: str = "M15"      # M5/M15
    sl_points: int = 250
    tp1_points: int = 500
    tp2_points: int = 1000
    box_lookback: int = 50     # จำนวนแท่งที่ใช้สร้างกรอบบน higher TF


# ---------- Helpers ----------
_TD_BASE = "https://api.twelvedata.com"

_TF_MAP = {
    # lower
    "M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min",
    # higher
    "H1": "1h", "H2": "2h", "H4": "4h",
    # day
    "D": "1day", "1D": "1day",
}

def _tf_to_interval(tf: str) -> str:
    tf = tf.upper().strip()
    if tf not in _TF_MAP:
        raise HTTPException(422, detail=f"Unsupported TF: {tf}")
    return _TF_MAP[tf]

def _fetch_td_series(symbol: str, interval: str, outputsize: int = 500) -> Dict[str, Any]:
    if not TWELVEDATA_API_KEY:
        raise HTTPException(500, detail="Missing TWELVEDATA_API_KEY")
    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": TWELVEDATA_API_KEY,
        "outputsize": str(outputsize),
        "format": "JSON",
        "dp": "6",
        "order": "ASC",
    }
    r = requests.get(f"{_TD_BASE}/time_series", params=params, timeout=25)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(502, detail="Upstream parse error from TwelveData")
    if "values" not in data:
        raise HTTPException(502, detail=f"TwelveData error: {data.get('message') or data}")
    return data

def _float(x: Any, default: float = math.nan) -> float:
    try:
        return float(x)
    except Exception:
        return default

def build_higher_box(values: list[dict], lookback: int = 50) -> Dict[str, float]:
    """สร้างกรอบจาก high/low ของ N แท่งสุดท้าย (higher TF)"""
    sub = values[-lookback:] if len(values) >= lookback else values
    highs = [_float(v.get("high")) for v in sub]
    lows  = [_float(v.get("low")) for v in sub]
    h_high = max(highs) if highs else math.nan
    h_low  = min(lows)  if lows  else math.nan
    return {"H_high": h_high, "H_low": h_low}

def last_price(values: list[dict]) -> float:
    return _float(values[-1]["close"]) if values else math.nan

def breakout_signal(h_high: float, h_low: float, last: float) -> Dict[str, Any]:
    """ตรรกะ Break + Close (เข้าเมื่อแท่งปิดอยู่นอกกรอบทันที)"""
    if any(math.isnan(x) for x in [h_high, h_low, last]):
        return {"status": "WAIT", "reason": "not_enough_data"}

    if last > h_high:
        return {"status": "ENTRY", "side": "LONG", "reason": "close_above_box"}
    if last < h_low:
        return {"status": "ENTRY", "side": "SHORT", "reason": "close_below_box"}
    return {"status": "WATCH", "reason": "ยังไม่ Breakout โซน H1/H4"}

def points_to_price(p: int) -> float:
    # ทองคำในแพลตฟอร์มส่วนใหญ่ 1 point = 0.01 (ปรับตามที่คุณใช้)
    return p * 0.01


# ---------- Routes ----------
@app.get("/")
def root():
    return {"app": APP_NAME, "version": APP_VERSION, "ok": True}

@app.post("/scan-signal")
def scan_signal(req: ScanReq):
    higher_iv = _tf_to_interval(req.higher_tf)
    lower_iv  = _tf_to_interval(req.lower_tf)

    # ดึงข้อมูล
    h_data = _fetch_td_series(req.symbol, higher_iv, outputsize=max(200, req.box_lookback + 5))
    l_data = _fetch_td_series(req.symbol, lower_iv,  outputsize=200)

    # สร้างกรอบจาก higher TF
    box = build_higher_box(h_data["values"], lookback=req.box_lookback)
    last = last_price(l_data["values"])

    sig = breakout_signal(box["H_high"], box["H_low"], last)

    resp: Dict[str, Any] = {
        "status": "OK",
        "signal": sig.get("status"),
        "reason": sig.get("reason"),
        "ref": {
            "higher_tf": req.higher_tf,
            "lower_tf": req.lower_tf,
            "H_high": round(box["H_high"], 2) if not math.isnan(box["H_high"]) else None,
            "H_low":  round(box["H_low"],  2) if not math.isnan(box["H_low"]) else None,
            "last":   round(last, 2) if not math.isnan(last) else None,
        },
        "params": {
            "sl_points": req.sl_points,
            "tp1_points": req.tp1_points,
            "tp2_points": req.tp2_points,
        },
    }

    if sig["status"] == "ENTRY":
        side = sig["side"]  # LONG / SHORT
        px = last
        sl = px - points_to_price(req.sl_points) if side == "LONG"  else px + points_to_price(req.sl_points)
        tp1 = px + points_to_price(req.tp1_points) if side == "LONG" else px - points_to_price(req.tp1_points)
        tp2 = px + points_to_price(req.tp2_points) if side == "LONG" else px - points_to_price(req.tp2_points)
        resp["entry"] = {
            "text": f"ENTRY {side} @ {px:.2f} | SL {sl:.2f} | TP1 {tp1:.2f} | TP2 {tp2:.2f}",
            "side": side,
            "price": round(px, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
        }

    return resp

@app.get("/health")
def health():
    return {"ok": True, "service": APP_NAME, "version": APP_VERSION}
