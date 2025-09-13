import os
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_VERSION = "2025-09-13.accesskey-stable"

# ---------- Config ----------
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").strip()
if not ALLOWED_ORIGINS or ALLOWED_ORIGINS == "*":
    CORS_ORIGINS = ["*"]
else:
    CORS_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]

# Access Key (เปิด/ปิดได้ด้วย ENV)
REQUIRE_ACCESS_KEY = os.getenv("REQUIRE_ACCESS_KEY", "false").lower() == "true"
VALID_KEYS = {k.strip() for k in os.getenv("ACCESS_KEYS", "").split(",") if k.strip()}

# ---------- FastAPI ----------
app = FastAPI(title="ForexPro Structure Scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Models ----------
class StructureRequest(BaseModel):
    symbol: str = Field(..., examples=["XAUUSD", "XAU/USD"])
    tfs: List[str] = Field(..., description="List of TFs: M5,M15,M30,H1,H4,D1", examples=[["M15", "H1"]])

class Candle(BaseModel):
    dt: str
    open: float
    high: float
    low: float
    close: float

# ---------- Utilities ----------
TF_MAP = {
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1day",
}

def normalize_symbol(sym: str) -> str:
    """XAUUSD -> XAU/USD, หรือคงไว้ถ้าเป็นรูปแบบ base/quote แล้ว"""
    s = (sym or "").strip().upper().replace(" ", "")
    if "/" in s:
        base, quote = s.split("/", 1)
        return f"{base}/{quote}"
    if len(s) == 6 and s.isalpha():
        return f"{s[:3]}/{s[3:]}"
    return s

def td_interval(tf: str) -> str:
    tfu = tf.upper()
    if tfu not in TF_MAP:
        raise HTTPException(status_code=400, detail=f"Unsupported TF: {tf}")
    return TF_MAP[tfu]

def fetch_series(symbol: str, tf: str, size: int = 320) -> List[Candle]:
    if not TD_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")
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
        r = requests.get(url, params=params, timeout=25)
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")
    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(status_code=502, detail=data.get("message", "API error"))
    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData.")
    candles: List[Candle] = []
    for v in values:
        try:
            candles.append(
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
    if not candles:
        raise HTTPException(status_code=502, detail="Cannot parse bars.")
    return candles  # latest first

def previous_closed(candles: List[Candle]) -> Candle:
    return candles[0]

# ---- SR: ใกล้ราคาปัจจุบัน (Resistance > close ใกล้สุด, Support < close ใกล้สุด) ----
def nearest_sr(candles: List[Candle], min_lookback: int = 150) -> Dict[str, Optional[float]]:
    arr = list(reversed(candles[:max(min_lookback, 60)]))  # asc
    highs, lows = [], []
    for i in range(1, len(arr) - 1):
        if arr[i].high > arr[i-1].high and arr[i].high > arr[i+1].high:
            highs.append(arr[i].high)
        if arr[i].low < arr[i-1].low and arr[i].low < arr[i+1].low:
            lows.append(arr[i].low)
    last_close = candles[0].close
    resistance, support = None, None
    highs_above = [h for h in highs if h > last_close]
    lows_below = [l for l in lows if l < last_close]
    if highs_above:
        resistance = min(highs_above)
    if lows_below:
        support = max(lows_below)
    return {"resistance": resistance, "support": support}

# ---- Order Blocks (เรียบง่าย: last opp-color candle before BOS 2-bar) ----
def detect_order_blocks(candles: List[Candle], max_blocks: int = 4) -> List[Dict[str, Any]]:
    arr = list(reversed(candles[:200]))  # asc
    res: List[Dict[str, Any]] = []
    for i in range(3, len(arr)):
        c0, c1, c2 = arr[i-3], arr[i-2], arr[i-1]
        c = arr[i]
        # Bullish OB: ปิดทะลุ high ของ 2 แท่งก่อนหน้า และ c2 เป็นแท่งแดง (close<open)
        if c.close > max(c0.high, c1.high) and c2.close < c2.open:
            low = min(c2.open, c2.close); high = max(c2.open, c2.close)
            if low < high:
                res.append({"type": "bullish", "low": round(low, 2), "high": round(high, 2)})
        # Bearish OB: ปิดหลุด low ของ 2 แท่งก่อนหน้า และ c2 เป็นแท่งเขียว (close>open)
        if c.close < min(c0.low, c1.low) and c2.close > c2.open:
            low = min(c2.open, c2.close); high = max(c2.open, c2.close)
            if low < high:
                res.append({"type": "bearish", "low": round(low, 2), "high": round(high, 2)})
        if len(res) >= max_blocks:
            break
    # คงเฉพาะช่วงสมบูรณ์และไม่ซ้ำ, ล่าสุดมาก่อน
    out, seen = [], set()
    for ob in reversed(res):
        key = (ob["type"], ob["low"], ob["high"])
        if key in seen: 
            continue
        seen.add(key)
        out.append(ob)
        if len(out) >= max_blocks:
            break
    return out

# ---- Access Control ----
def check_access(x_access_key: Optional[str]):
    if not REQUIRE_ACCESS_KEY:
        return  # public mode
    if not x_access_key or x_access_key not in VALID_KEYS:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid access key")

# ---------- Routes ----------
@app.get("/")
def root():
    return {"app": "ForexPro Scanner", "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

@app.post("/structure")
def structure(req: StructureRequest, x_access_key: Optional[str] = Header(default=None)):
    check_access(x_access_key)

    symbol = normalize_symbol(req.symbol)
    out: Dict[str, Any] = {
        "status": "OK",
        "symbol": symbol,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "results": [],
    }
    if not req.tfs:
        raise HTTPException(status_code=400, detail="tfs is required")

    for tf in req.tfs:
        tfu = tf.upper()
        try:
            candles = fetch_series(symbol, tfu, 320)
            last = previous_closed(candles)
            sr = nearest_sr(candles, 150)
            obs = detect_order_blocks(candles, 4)
            result = {
                "tf": tfu,
                "last": {
                    "dt": last.dt,
                    "open": last.open,
                    "high": last.high,
                    "low": last.low,
                    "close": last.close,
                },
                "resistance": round(sr["resistance"], 2) if sr["resistance"] is not None else None,
                "support": round(sr["support"], 2) if sr["support"] is not None else None,
                "order_blocks": obs,
            }
        except HTTPException:
            raise
        except Exception as e:
            result = {"tf": tfu, "error": str(e)}
        out["results"].append(result)
    return out
