import os
import math
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any

# ---------------------------
# Config & helpers
# ---------------------------

API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
if not API_KEY:
    # ไม่หยุดแอป เพื่อให้ /health ตอบได้ แต่ /signal จะ error ตรงจุดเดียว
    pass

ALLOWED_ORIGINS_RAW = os.getenv("ALLOWED_ORIGINS", "*").strip()
if ALLOWED_ORIGINS_RAW == "" or ALLOWED_ORIGINS_RAW == "*":
    ALLOWED_ORIGINS = ["*"]
else:
    ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS_RAW.split(",") if o.strip()]

# TwelveData interval mapping
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

def interval_to_td(tf: str) -> str:
    tf = tf.upper().strip()
    if tf not in INTERVAL_MAP:
        raise HTTPException(status_code=422, detail=f"Unsupported timeframe: {tf}")
    return INTERVAL_MAP[tf]

def pip_size_for(symbol: str) -> float:
    """
    กำหนดขนาด 'จุด' (point) ต่อราคาจริง
    - XAU/USD, XAG/USD และพวกสินค้าโภคภัณฑ์หลายโบรก: 1 จุด = 0.01
    - คู่เงินหลัก: 1 จุด = 0.0001 (แต่หลายโบรกเรียก 'pip' = 0.0001, 'point' = 0.00001)
    เราใช้ heuristic แบบปลอดภัยว่า:
      - ถ้าเจอ XAU หรือ XAG → 0.01
      - ถ้าเป็น XXX/JPY → 0.01
      - อื่น ๆ → 0.0001
    """
    s = symbol.upper()
    if "XAU" in s or "XAG" in s or "GOLD" in s:
        return 0.01
    if s.endswith("/JPY"):
        return 0.01
    return 0.0001

def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(price / tick) * tick

def fetch_last_closed_bar(symbol: str, interval: str) -> Dict[str, Any]:
    """
    ดึงแท่งล่าสุด 'ที่ปิดแล้ว' จาก TwelveData
    TwelveData จะส่งแท่งล่าสุดที่กำลังก่อตัวอยู่มาเป็นลำดับแรกเสมอในหลายกรณี
    เราจึงดึง outputsize=2 แล้วใช้ลำดับที่สองเป็น 'แท่งปิดแล้ว'
    """
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": 2,
        "format": "JSON",
        "apikey": API_KEY,
        "order": "desc",  # เรียงใหม่สุด -> เก่าสุด
    }
    r = requests.get(url, params=params, timeout=15)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"TwelveData non-JSON response: HTTP {r.status_code}")

    if "status" in data and data["status"] == "error":
        raise HTTPException(status_code=502, detail=f"TwelveData error: {data.get('message', 'unknown')}")

    values = data.get("values")
    if not values or len(values) < 2:
        raise HTTPException(status_code=502, detail="Not enough bars from TwelveData")

    # index 0 = แท่งปัจจุบัน (อาจยังไม่ปิด), index 1 = แท่งปิดล่าสุด
    bar = values[1]
    # แปลงเป็น float ชัดเจน
    try:
        return {
            "datetime": bar.get("datetime"),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
        }
    except Exception:
        raise HTTPException(status_code=502, detail="Malformed bar data from TwelveData")

# ---------------------------
# API
# ---------------------------

app = FastAPI(title="xau-scanner", version="2025-09-09.3")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SignalIn(BaseModel):
    symbol: str = Field(..., examples=["XAU/USD", "EUR/USD"])
    tf_high: str = Field(..., examples=["H4", "H1"])
    tf_low: str = Field(..., examples=["M15", "M5"])
    sl: int = Field(..., description="Stop loss in points", examples=[250])
    tp1: int = Field(..., description="TP1 in points", examples=[500])
    tp2: int = Field(..., description="TP2 in points", examples=[1000])

class SignalOut(BaseModel):
    status: str
    message: str
    signal: str
    entry_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp1_price: Optional[float] = None
    tp2_price: Optional[float] = None
    box_upper: Optional[float] = None
    box_lower: Optional[float] = None
    reason: Dict[str, Any] = {}

@app.get("/")
def root():
    return {"app": "xau-scanner", "version": app.version, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "api_key": bool(API_KEY)}

@app.post("/signal", response_model=SignalOut)
def signal(payload: SignalIn):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="TWELVEDATA_API_KEY is not set")

    symbol = payload.symbol.strip()
    int_high = interval_to_td(payload.tf_high)
    int_low = interval_to_td(payload.tf_low)

    # 1) เอา 'กรอบ' จากแท่งที่ปิดแล้วของ TF สูง
    high_bar = fetch_last_closed_bar(symbol, int_high)
    box_upper = high_bar["high"]
    box_lower = high_bar["low"]

    # 2) ดูแท่งที่ปิดแล้วของ TF ต่ำ
    low_bar = fetch_last_closed_bar(symbol, int_low)
    low_close = low_bar["close"]

    direction = "WAIT"
    reason: Dict[str, Any] = {
        "tf_high": payload.tf_high,
        "tf_low": payload.tf_low,
        "high_bar": high_bar,
        "low_bar": low_bar,
        "logic": "Break + Close ทันที จากกรอบ TF สูง (ใช้ high/low ของแท่งปิดล่าสุด)"
    }

    if low_close > box_upper:
        direction = "LONG"
    elif low_close < box_lower:
        direction = "SHORT"

    # ถ้าไม่เบรกเอาต์ ก็รอ
    if direction == "WAIT":
        msg = "WAIT — ราคายังปิดอยู่ในกรอบ"
        return SignalOut(
            status="OK",
            message=msg,
            signal="WAIT",
            box_upper=box_upper,
            box_lower=box_lower,
            reason=reason,
        )

    # 3) คำนวณราคา SL/TP จาก points
    pt = pip_size_for(symbol)
    entry = low_close  # เข้าเมื่อแท่ง TF ต่ำ 'ปิด' นอกกรอบ
    if direction == "LONG":
        sl_price = round_to_tick(entry - payload.sl * pt, pt)
        tp1_price = round_to_tick(entry + payload.tp1 * pt, pt)
        tp2_price = round_to_tick(entry + payload.tp2 * pt, pt)
    else:  # SHORT
        sl_price = round_to_tick(entry + payload.sl * pt, pt)
        tp1_price = round_to_tick(entry - payload.tp1 * pt, pt)
        tp2_price = round_to_tick(entry - payload.tp2 * pt, pt)

    # 4) สร้างข้อความตามฟอร์แมตที่ต้องการ
    fmt = lambda x: f"{x:.2f}" if pt >= 0.01 else f"{x:.5f}"
    msg = (
        f"ENTRY {direction} @ {fmt(entry)} | "
        f"SL {fmt(sl_price)} | TP1 {fmt(tp1_price)} | TP2 {fmt(tp2_price)}"
    )

    reason.update({
        "point_size": pt,
        "params": {
            "sl_points": payload.sl,
            "tp1_points": payload.tp1,
            "tp2_points": payload.tp2,
        }
    })

    return SignalOut(
        status="OK",
        message=msg,
        signal=direction,
        entry_price=entry,
        sl_price=sl_price,
        tp1_price=tp1_price,
        tp2_price=tp2_price,
        box_upper=box_upper,
        box_lower=box_lower,
        reason=reason,
    )
