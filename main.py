import os
import math
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
import requests
from datetime import datetime

APP_VERSION = "2025-09-12.2"

TD_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
if not TD_KEY:
    # ให้รันขึ้น แต่หากเรียก /structure จะ error ชัดเจน
    pass

_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _ALLOWED in ("", "*"):
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

app = FastAPI(title="xau-scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Models ----------
class StructureRequest(BaseModel):
    symbol: str = Field(..., examples=["XAUUSD", "XAU/USD"])
    tfs: List[str] = Field(..., examples=[["M5", "M15", "M30", "H1", "H4", "D1"]])

    @field_validator("tfs")
    @classmethod
    def check_tfs(cls, v: List[str]) -> List[str]:
        allowed = {"M5", "M15", "M30", "H1", "H4", "D1"}
        vv = [x.upper() for x in v]
        for x in vv:
            if x not in allowed:
                raise ValueError(f"Unsupported TF: {x}")
        return vv


# ---------- Helpers ----------
def normalize_symbol(s: str) -> str:
    s = s.strip().upper().replace(" ", "")
    # ตัวอย่าง: XAUUSD -> XAU/USD
    if "/" not in s and len(s) >= 6:
        base = s[:3]
        quote = s[3:]
        s = f"{base}/{quote}"
    return s

def td_interval(tf: str) -> str:
    m = tf.upper()
    if m == "M5": return "5min"
    if m == "M15": return "15min"
    if m == "M30": return "30min"
    if m == "H1": return "1h"
    if m == "H4": return "4h"
    if m == "D1": return "1day"
    raise ValueError(f"Unsupported TF {tf}")

def fetch_series(symbol: str, tf: str, size: int = 250) -> List[Dict[str, Any]]:
    if not TD_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",
        "timezone": "UTC",
        "apikey": TD_KEY,
    }
    r = requests.get(url, params=params, timeout=25)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON")

    if isinstance(data, dict) and data.get("status") == "error":
        # ส่งผ่าน error ของ TwelveData กลับไปให้เห็นชัด
        raise HTTPException(status_code=502, detail=data.get("message", "API error"))

    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData")

    bars: List[Dict[str, Any]] = []
    for v in values:
        try:
            bars.append({
                "dt": v["datetime"],
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
            })
        except Exception:
            # ข้ามแถวที่พาร์สไม่ได้
            continue

    if not bars:
        raise HTTPException(status_code=502, detail="Cannot parse bars")
    # TwelveData order=desc => แท่งล่าสุดอยู่ index 0
    return bars

def is_pivot_high(bars: List[Dict[str, Any]], i: int, left: int, right: int) -> bool:
    h = bars[i]["high"]
    for k in range(i - left, i + right + 1):
        if k == i or k < 0 or k >= len(bars):
            continue
        if bars[k]["high"] >= h:
            return False
    return True

def is_pivot_low(bars: List[Dict[str, Any]], i: int, left: int, right: int) -> bool:
    l = bars[i]["low"]
    for k in range(i - left, i + right + 1):
        if k == i or k < 0 or k >= len(bars):
            continue
        if bars[k]["low"] <= l:
            return False
    return True

def swing_points(bars: List[Dict[str, Any]], left: int = 2, right: int = 2, max_points: int = 60):
    highs: List[float] = []
    lows: List[float] = []
    # ใช้เฉพาะส่วนต้นของลิสต์ (แท่งล่าสุดๆ) 220 แท่งเพื่อเร็วขึ้น
    rng = range(min(len(bars)-1, 219), -1, -1)
    for i in rng:
        if is_pivot_high(bars, i, left, right):
            highs.append(bars[i]["high"])
        if is_pivot_low(bars, i, left, right):
            lows.append(bars[i]["low"])
        if len(highs) >= max_points and len(lows) >= max_points:
            break
    return highs, lows

def find_order_blocks(bars: List[Dict[str, Any]]) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    heuristic เบาๆ:
    - Bullish OB: หาแท่งแดง (close<open) แล้วแท่งถัดไปปิดสูงกว่า high ของแท่งแดง => zone = [low(แดง), open(แดง)]
    - Bearish OB: หาแท่งเขียว (close>open) แล้วแท่งถัดไปปิดต่ำกว่า low ของแท่งเขียว => zone = [open(เขียว), high(เขียว)]
    ส่งกลับเฉพาะล่าสุด (อย่างละ 1)
    """
    bull = None
    bear = None
    # ใช้ 120 แท่งล่าสุด
    N = min(len(bars)-1, 119)
    for i in range(N, 0, -1):
        cur = bars[i]
        nxt = bars[i-1]  # เพราะ desc: i-1 คือถัดไป
        # Bullish
        if cur["close"] < cur["open"] and nxt["close"] > cur["high"]:
            bull = {
                "type": "bullish",
                "zone": [round(cur["low"], 2), round(cur["open"], 2)],
                "bar": cur
            }
            break
    N = min(len(bars)-1, 119)
    for i in range(N, 0, -1):
        cur = bars[i]
        nxt = bars[i-1]
        # Bearish
        if cur["close"] > cur["open"] and nxt["close"] < cur["low"]:
            bear = {
                "type": "bearish",
                "zone": [round(cur["open"], 2), round(cur["high"], 2)],
                "bar": cur
            }
            break
    return {"bullish": bull, "bearish": bear}

def analyze_tf(bars: List[Dict[str, Any]], tf: str) -> Dict[str, Any]:
    last = bars[0]
    # หา swing high/low
    highs, lows = swing_points(bars, left=2, right=2, max_points=80)
    # resistance/support ตัวหลักเอาอันแรกของลิสต์ (ล่าสุด)
    resistance = round(highs[0], 2) if highs else None
    support    = round(lows[0], 2)  if lows  else None

    # order blocks
    obs = find_order_blocks(bars)
    return {
        "tf": tf,
        "last_bar": last,
        "resistance": resistance,
        "support": support,
        "resistances": [round(x, 2) for x in highs[:5]],
        "supports":    [round(x, 2) for x in lows[:5]],
        "order_blocks": obs
    }

# ---------- Routes ----------
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat() + "Z"}

@app.post("/structure")
def structure(req: StructureRequest):
    symbol = normalize_symbol(req.symbol)
    out: Dict[str, Any] = {"status": "OK", "symbol": symbol, "results": []}
    try:
        for tf in req.tfs:
            bars = fetch_series(symbol, tf, size=250)
            out["results"].append(analyze_tf(bars, tf))
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
