# main.py  — XAU Scanner (compat tf / breakout + retest / 50% pullback)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Literal, Dict, Any
import os, math, time
import requests
from datetime import datetime, timedelta, timezone

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGINS] if ALLOWED_ORIGINS != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Models ----------
TF_HIGH = Literal["H1","H4"]
TF_LOW  = Literal["M5","M15"]

class SignalReq(BaseModel):
    symbol: str
    # รับได้ทั้งรูปแบบเก่า/ใหม่: ถ้าไม่ได้ส่ง tf_high/tf_low มา ให้ใช้ tf เดี่ยว
    tf_high: Optional[TF_HIGH] = None
    tf_low: Optional[TF_LOW]   = None
    tf: Optional[TF_LOW]       = None  # เผื่อฟรอนต์ส่งมารูปแบบเก่า

# ---------- TwelveData ----------
def _tw_url(endpoint: str, **params) -> str:
    base = f"https://api.twelvedata.com/{endpoint}"
    q = "&".join([f"{k}={v}" for k,v in params.items() if v is not None])
    return f"{base}?{q}&apikey={TWELVEDATA_API_KEY}"

def get_ohlc(symbol: str, interval: str, limit: int = 120):
    url = _tw_url("time_series", symbol=symbol, interval=interval, outputsize=limit, timezone="UTC")
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    js = r.json()
    data = js.get("values") or []
    # Normalize order: newest last
    data = list(reversed(data))
    # convert numeric
    for d in data:
        for k in ("open","high","low","close"):
            d[k] = float(d[k])
        d["dt"] = datetime.fromisoformat(d["datetime"].replace("Z","+00:00"))
    return data

# ---------- Core logic ----------
def build_box_prev_bar(data_h: list[Dict[str,Any]]):
    """กล่อง = high/low ของ 'แท่งก่อนหน้า' ใน TF สูง (H1/H4)"""
    if len(data_h) < 2:
        return None
    prev = data_h[-2]  # previous bar
    return {
        "upper": prev["high"],
        "lower": prev["low"],
        "bar": {
            "datetime": prev["dt"].isoformat(),
            "open": prev["open"], "high": prev["high"],
            "low": prev["low"], "close": prev["close"]
        }
    }

def breakout_signal(data_l: list[Dict[str,Any]], box: Dict[str,Any]):
    """หาการเบรกกล่องจาก TF ต่ำ (M5/M15). ถ้าพบ ให้คืนรายละเอียด + จุดเข้าแบบรีเทสต์/50%"""
    if not data_l or not box: 
        return None

    up, lo = box["upper"], box["lower"]
    last = data_l[-1]
    prev = data_l[-2] if len(data_l)>=2 else None

    # เงื่อนไขเบรก: close ข้ามกรอบ + มีการปิดนอกกรอบ
    if last["close"] > up:
        side = "BUY"
        brk_bar = last
        # entry แบบรีเทสต์เส้นบน
        retest = up
        # entry แบบ 50% ของแท่งเบรก
        fifty = (brk_bar["open"] + brk_bar["close"]) / 2.0
        entry = {"side": side, "by_retest": retest, "by_50pct": fifty}
        # SL ใช้ฝั่งตรงข้ามของกล่อง หรือ Low ของแท่งเบรก (เลือกปลอดภัยกว่า)
        sl = min(lo, brk_bar["low"])
        # TP เสนอแบบระยะสั้นใน 2–3 ชม.: ใช้ความสูงของกล่อง
        box_h = max(5.0, up - lo)
        tp1 = retest + 0.5*box_h
        tp2 = retest + 1.0*box_h
        return {"side": side, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "break_bar": brk_bar}

    if last["close"] < lo:
        side = "SELL"
        brk_bar = last
        retest = lo
        fifty = (brk_bar["open"] + brk_bar["close"]) / 2.0
        entry = {"side": side, "by_retest": retest, "by_50pct": fifty}
        sl = max(up, brk_bar["high"])
        box_h = max(5.0, up - lo)
        tp1 = retest - 0.5*box_h
        tp2 = retest - 1.0*box_h
        return {"side": side, "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "break_bar": brk_bar}

    return None

# ---------- API ----------
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": "2025-09-09.3", "ok": True}

@app.post("/signal")
def signal(req: SignalReq):
    # ทำให้รองรับทั้งสองรูปแบบ
    tf_low = req.tf_low or req.tf
    tf_high = req.tf_high or "H4"  # ค่าปริยาย = H4

    if tf_low not in ("M5","M15"):
        return {"status":"ERROR","message":"tf_low/tf must be M5 or M15"}
    if tf_high not in ("H1","H4"):
        return {"status":"ERROR","message":"tf_high must be H1 or H4"}

    try:
        # ดึงข้อมูล
        data_high = get_ohlc(req.symbol, interval=tf_high, limit=20)
        data_low  = get_ohlc(req.symbol, interval=tf_low,  limit=60)  # ~2–3 ชั่วโมง @ M5

        box = build_box_prev_bar(data_high)
        if not box:
            return {"status":"ERROR","message":"Not enough high timeframe data."}

        sig = breakout_signal(data_low, box)

        # สร้าง S/R ใกล้ ๆ (จาก highs/lows ย้อนหลัง ~36 bars)
        lookback = 36 if tf_low=="M5" else 24
        tail = data_low[-lookback:] if len(data_low)>lookback else data_low
        resistance = max(d["high"] for d in tail)
        support    = min(d["low"]  for d in tail)

        out = {
            "status": "OK",
            "symbol": req.symbol,
            "tf_high": tf_high,
            "tf_low": tf_low,
            "box": {"upper": box["upper"], "lower": box["lower"], "built_from": box["bar"]["datetime"]},
            "sr": {"support": support, "resistance": resistance},
        }

        if sig:
            out.update({
                "signal": sig["side"],
                "entry": sig["entry"],
                "sl": sig["sl"],
                "tp1": sig["tp1"],
                "tp2": sig["tp2"],
                "note": "เข้าที่รีเทสต์เส้นกล่องหรือ 50% ของแท่งเบรก"
            })
        else:
            last = data_low[-1]
            msg = "WAIT — รอเบรกกรอบบน/ล่าง"
            out.update({"signal": None, "message": msg, "last": {
                "datetime": last["dt"].isoformat(),
                "open": last["open"], "high": last["high"], "low": last["low"], "close": last["close"]
            }})

        return out

    except requests.HTTPError as e:
        return {"status":"ERROR","message":"TwelveData HTTP error","detail":str(e)}
    except Exception as e:
        return {"status":"ERROR","message":"Unhandled error","detail":str(e)}
