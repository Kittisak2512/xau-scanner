# main.py
import os
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-12.RS-OB-REAL-1"

# ============== ENV / CORS =================
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOW_ORIGINS = ["*"] if _ALLOWED in ("", "") else [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# ============== APP ========================
app = FastAPI(title="xau-structure", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============== MODELS =====================
class StructureRequest(BaseModel):
    symbol: str = Field(..., examples=["XAUUSD", "XAU/USD"])
    tfs: List[str] = Field(..., description="TFs เช่น ['M5','M15','M30','H1','H4','D1']")

    @field_validator("tfs")
    @classmethod
    def check_tfs(cls, v: List[str]) -> List[str]:
        ok = {"M5", "M15", "M30", "H1", "H4", "D1"}
        vv = [s.upper().strip() for s in v]
        bad = [s for s in vv if s not in ok]
        if bad:
            raise ValueError(f"Unsupported TF(s): {bad}. Allowed: {sorted(ok)}")
        # unique by order
        seen = set(); out = []
        for x in vv:
            if x not in seen:
                out.append(x); seen.add(x)
        return out

@dataclass
class Candle:
    dt: str
    open: float
    high: float
    low: float
    close: float

# ============== UTILS ======================
def normalize_symbol(s: str) -> str:
    """แปลงสัญลักษณ์ให้ TwelveData เข้าใจได้แน่นอน"""
    s = (s or "").strip().upper()
    aliases = {"XAUUSD": "XAU/USD", "XAU-USD": "XAU/USD", "XAU_USD": "XAU/USD", "GOLD": "XAU/USD"}
    if s in aliases:
        return aliases[s]
    if " " in s and s.replace(" ", "") == "XAUUSD":
        return "XAU/USD"
    if "/" not in s and len(s) == 6:
        return s[:3] + "/" + s[3:]
    return s

def tf_to_interval(tf: str) -> str:
    m = tf.upper()
    mapping = {"M5":"5min","M15":"15min","M30":"30min","H1":"1h","H4":"4h","D1":"1day"}
    if m not in mapping:
        raise HTTPException(status_code=400, detail=f"Unsupported TF: {tf}")
    return mapping[m]

def fetch_series(symbol: str, tf: str, size: int = 320) -> List[Candle]:
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": tf_to_interval(tf),
        "outputsize": size,
        "order": "desc",      # ล่าสุดก่อน
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=25)
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON")

    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(status_code=502, detail=data.get("message","Upstream error"))

    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData")

    out: List[Candle] = []
    for v in values:
        try:
            out.append(Candle(
                dt=v["datetime"],
                open=float(v["open"]),
                high=float(v["high"]),
                low=float(v["low"]),
                close=float(v["close"]),
            ))
        except Exception:
            continue
    if not out:
        raise HTTPException(status_code=502, detail="Cannot parse candles")
    return out  # ล่าสุด -> เก่า

# ============== CORE LOGIC =================
def find_swings(bars: List[Candle], lookback: int = 200) -> Dict[str, List[float]]:
    """
    หา swing-high / swing-low จริงจากโครงสร้างราคา:
      - swing-high: H[i] > H[i-1] และ H[i] > H[i+1]
      - swing-low : L[i] < L[i-1] และ L[i] < L[i+1]
    ใช้เฉพาะช่วงย้อนหลัง (ไม่ตั้ง buffer เอง)
    """
    seq = list(reversed(bars[:lookback+2]))  # เก่าสุด -> ล่าสุด
    highs, lows = [], []
    for i in range(1, len(seq)-1):
        a, b, c = seq[i-1], seq[i], seq[i+1]
        if b.high > a.high and b.high > c.high:
            highs.append(b.high)
        if b.low < a.low and b.low < c.low:
            lows.append(b.low)
    # เรียงและเอาค่าไม่ซ้ำ
    highs = sorted(set(round(x, 2) for x in highs))
    lows  = sorted(set(round(x, 2) for x in lows))
    return {"highs": highs, "lows": lows}

def nearest_rs_from_swings(bars: List[Candle]) -> Dict[str, Optional[float]]:
    """
    เลือก R/S จาก swings ที่ 'อยู่คนละฝั่งกับราคาปัจจุบัน' และใกล้ที่สุด
    ไม่มีการตั้ง +/− ระยะเอง
    """
    if not bars:
        return {"resistance": None, "support": None}

    last_close = bars[0].close
    swings = find_swings(bars, lookback=220)
    highs = [h for h in swings["highs"] if h > last_close]
    lows  = [l for l in swings["lows"]  if l < last_close]

    resistance = min(highs) if highs else None
    support    = max(lows)  if lows  else None
    return {"resistance": resistance, "support": support}

def detect_order_blocks(bars: List[Candle], lookback: int = 160) -> List[Dict[str, Any]]:
    """
    OB จริง แบบ conservative:
      - Bullish OB: หา 'แท่งแดง' A ที่ก่อนหน้าเกิด impulse ขึ้น (แท่งถัด ๆ ไปยืนเหนือ high ย้อนหลัง)
                    และ 'โซนของ A' (min(open,close)..max(open,close)) ต้อง 'ต่ำกว่าราคา' ปัจจุบัน
      - Bearish OB: ตรงข้ามกับขาลง และโซนต้อง 'สูงกว่าราคา' ปัจจุบัน
    ถ้าไม่พบ คืน []
    """
    out: List[Dict[str, Any]] = []
    if len(bars) < 20:
        return out

    last_close = bars[0].close
    seq = list(reversed(bars[:lookback+6]))  # เก่าสุด -> ล่าสุด

    def box_of(c: Candle):
        lo = min(c.open, c.close)
        hi = max(c.open, c.close)
        return round(lo, 2), round(hi, 2)

    # เดินจากล่าสุดย้อนกลับเพื่อหา zone ที่ยังสัมพันธ์กับราคา
    for i in range(2, len(seq)-3):
        a = seq[i]     # แท่งผู้ต้องสงสัย
        b, c, d = seq[i+1], seq[i+2], seq[i+3]

        # Bullish OB: A เป็นแดง และเกิด impulse ขึ้นหลังจากนั้น (ยืนเหนือ high ย้อนหลัง)
        if a.close < a.open:
            window_high = max(x.high for x in seq[i+1:i+5])
            if d.close > window_high:
                lo, hi = box_of(a)
                # ต้องอยู่ต่ำกว่าราคา (เป็น demand ใต้ราคา)
                if hi <= last_close:
                    out.append({"type": "bullish", "zone": [lo, hi]})

        # Bearish OB: A เป็นเขียว และเกิด impulse ลงหลังจากนั้น (ยืนต่ำกว่า low ย้อนหลัง)
        if a.close > a.open:
            window_low = min(x.low for x in seq[i+1:i+5])
            if d.close < window_low:
                lo, hi = box_of(a)
                # ต้องอยู่สูงกว่าราคา (เป็น supply เหนือราคา)
                if lo >= last_close:
                    out.append({"type": "bearish", "zone": [lo, hi]})

    # เอาโซนที่ใกล้ราคาสุดก่อน และตัดไม่เกิน 4
    def dist(z):
        lo, hi = z["zone"]
        if z["type"] == "bullish":
            # โซนอยู่ใต้ราคา → ใกล้ = hi ใกล้ last_close
            return abs(last_close - hi)
        else:
            # โซนอยู่เหนือราคา → ใกล้ = lo ใกล้ last_close
            return abs(lo - last_close)

    out.sort(key=dist)
    return out[:4]

def build_tf_block(symbol: str, tf: str) -> Dict[str, Any]:
    series = fetch_series(symbol, tf, size=320)
    last = series[0]

    rs = nearest_rs_from_swings(series)
    # ไม่ฝืนตั้งค่าเอง ถ้าไม่เจอจริง ๆ ให้เป็น None เพื่อให้ UI แสดง "—"
    resistance = rs["resistance"]
    support    = rs["support"]

    obs = detect_order_blocks(series)

    return {
        "tf": tf,
        "last_bar": {
            "dt": last.dt,
            "open": round(last.open, 2),
            "high": round(last.high, 2),
            "low": round(last.low, 2),
            "close": round(last.close, 2),
        },
        "resistance": resistance if resistance is not None else None,
        "support":    support    if support    is not None else None,
        "order_blocks": obs,   # [] ถ้าไม่พบ
    }

# ============== ROUTES =====================
@app.get("/")
def root():
    return {"app": "xau-structure", "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

@app.post("/structure")
def structure(req: StructureRequest):
    symbol = normalize_symbol(req.symbol)
    results: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []

    for tf in req.tfs:
        try:
            results.append(build_tf_block(symbol, tf))
        except HTTPException as he:
            errors.append({"tf": tf, "error": str(he.detail)})
        except Exception as e:
            errors.append({"tf": tf, "error": str(e)})

    status = "OK"
    if errors and results:
        status = "PARTIAL"
    elif errors and not results:
        status = "ERROR"

    return {
        "status": status,
        "symbol": symbol,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "errors": errors,
    }
