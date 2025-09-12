# main.py
import os
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import requests
from datetime import datetime, timezone

APP_VERSION = "2025-09-12.3"

# ---------- Config ----------
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOW_ORIGINS = ["*"] if ALLOWED in ("", "*") else [o.strip() for o in ALLOWED.split(",") if o.strip()]

# ---------- FastAPI ----------
app = FastAPI(title="xau-scanner", version=APP_VERSION)
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

@dataclass
class Candle:
    dt: str
    open: float
    high: float
    low: float
    close: float

# ---------- Utils ----------
def normalize_symbol(sym: str) -> str:
    s = sym.upper().replace(" ", "")
    if "/" not in s and len(s) >= 6:
        # พวก XAUUSD, EURUSD => ใส่ /
        s = s[:3] + "/" + s[3:]
    return s

def td_interval(tf: str) -> str:
    m = tf.upper()
    mapping = {
        "M5": "5min",
        "M15": "15min",
        "M30": "30min",
        "H1": "1h",
        "H4": "4h",
        "D1": "1day",
    }
    if m not in mapping:
        raise HTTPException(status_code=400, detail=f"Unsupported timeframe: {tf}")
    return mapping[m]

def fetch_series(symbol: str, tf: str, size: int = 300) -> List[Candle]:
    if not TD_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",      # latest first
        "timezone": "UTC",
        "apikey": TD_API_KEY,
    }
    r = requests.get(url, params=params, timeout=20)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON.")

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

# สวิงไฮ/โล แบบพื้นฐาน: prev < cur > next (swing high), prev > cur < next (swing low)
def swing_points(bars: List[Candle], lookback: int = 200) -> Dict[str, List[float]]:
    seq = list(reversed(bars[: lookback + 2]))  # เก่าสุด -> ล่าสุด
    highs: List[float] = []
    lows: List[float] = []
    for i in range(1, len(seq) - 1):
        a, b, c = seq[i - 1], seq[i], seq[i + 1]
        if b.high > a.high and b.high > c.high:
            highs.append(b.high)
        if b.low < a.low and b.low < c.low:
            lows.append(b.low)
    return {"highs": highs, "lows": lows}

def nearest_levels_from_swings(bars: List[Candle]) -> Dict[str, Optional[float]]:
    last_close = bars[0].close
    sp = swing_points(bars, lookback=220)
    highs = sorted(set(sp["highs"]))
    lows = sorted(set(sp["lows"]))

    # resistance: ไฮที่อยู่ "เหนือ" close และใกล้ที่สุด
    resistance_candidates = [h for h in highs if h > last_close]
    resistance = min(resistance_candidates) if resistance_candidates else None

    # support: โลที่อยู่ "ต่ำกว่า" close และใกล้ที่สุด
    support_candidates = [l for l in lows if l < last_close]
    support = max(support_candidates) if support_candidates else None

    # fallback (เผื่อ lookback ไม่เจอ)
    if resistance is None:
        resistance = max(x.high for x in bars[:100])
        if resistance <= last_close:
            resistance = last_close + 5  # กันไม่ให้ต่ำกว่า close
    if support is None:
        support = min(x.low for x in bars[:100])
        if support >= last_close:
            support = last_close - 5

    return {"support": round(support, 2), "resistance": round(resistance, 2), "close": last_close}

def detect_order_blocks(bars: List[Candle], lookback: int = 150) -> List[Dict[str, Any]]:
    """ค้นหา OB แบบเรียบง่าย และป้องกันค่า null/null"""
    zones: List[Dict[str, Any]] = []
    n = min(len(bars), lookback + 3)
    seq = list(reversed(bars[:n]))  # เก่าสุด -> ล่าสุด

    for i in range(len(seq) - 2):
        a, b, c = seq[i], seq[i + 1], seq[i + 2]

        # Bullish OB: down → up → up (และมี expansion ต่อเนื่อง)
        if a.open > a.close and b.close > b.open and c.close > c.open and b.high < c.high:
            low, high = min(a.open, a.close), max(a.open, a.close)
            if low is not None and high is not None:
                zones.append({"type": "bullish", "zone": [round(low, 2), round(high, 2)]})

        # Bearish OB: up → down → down
        if a.close > a.open and b.close < b.open and c.close < c.open and b.low > c.low:
            low, high = min(a.open, a.close), max(a.open, a.close)
            if low is not None and high is not None:
                zones.append({"type": "bearish", "zone": [round(low, 2), round(high, 2)]})

    return zones[-4:]  # เอาเฉพาะล่าสุดไม่เกิน 4 โซน

# ---------- Routes ----------
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

@app.post("/structure")
def structure(req: StructureRequest):
    symbol = normalize_symbol(req.symbol)
    tfs = [tf.upper() for tf in req.tfs]
    supported = {"M5", "M15", "M30", "H1", "H4", "D1"}
    for tf in tfs:
        if tf not in supported:
            raise HTTPException(status_code=400, detail=f"Unsupported TF: {tf}")

    out: Dict[str, Any] = {
        "status": "OK",
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "results": [],
        "errors": [],
    }

    for tf in tfs:
        try:
            bars = fetch_series(symbol, tf, size=320)
            last = bars[0]
            levels = nearest_levels_from_swings(bars)

            # บังคับเงื่อนไข: resistance > close, support < close
            close = last.close
            resistance = levels["resistance"]
            support = levels["support"]
            if resistance <= close:
                resistance = round(close + 5, 2)
            if support >= close:
                support = round(close - 5, 2)

            obs = detect_order_blocks(bars, lookback=150)

            out["results"].append(
                {
                    "tf": tf,
                    "last_bar": {
                        "dt": last.dt,
                        "open": round(last.open, 2),
                        "high": round(last.high, 2),
                        "low": round(last.low, 2),
                        "close": round(last.close, 2),
                    },
                    "resistance": resistance,
                    "support": support,
                    "order_blocks": obs,  # ถ้าไม่เจอ = []
                }
            )
        except HTTPException as he:
            out["errors"].append({"tf": tf, "error": he.detail})
        except Exception as e:
            out["errors"].append({"tf": tf, "error": str(e)})

    # ถ้าบาง TF ล้มเหลว
    if out["errors"] and out["results"]:
        out["status"] = "PARTIAL"
    elif out["errors"] and not out["results"]:
        out["status"] = "ERROR"

    return out
