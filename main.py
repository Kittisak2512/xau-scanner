import os
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from datetime import datetime, timezone
import requests

APP_VERSION = "2025-09-12.1"

# --------------------
# Config / ENV
# --------------------
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOW_ORIGINS = ["*"] if (ALLOWED == "*" or ALLOWED == "") else [o.strip() for o in ALLOWED.split(",") if o.strip()]

# --------------------
# App
# --------------------
app = FastAPI(title="xau-scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------
# Models
# --------------------
ALLOWED_TFS = {"M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h", "D1": "1day"}

class StructureRequest(BaseModel):
    symbol: str = Field(..., examples=["XAU/USD", "XAUUSD"])
    tfs: List[str] = Field(..., examples=[["M5","M15","M30","H1","H4","D1"]])

    @field_validator("tfs")
    @classmethod
    def validate_tfs(cls, v: List[str]) -> List[str]:
        cleaned = []
        for tf in v:
            t = tf.upper()
            if t not in ALLOWED_TFS:
                raise ValueError(f"Unsupported TF: {tf}")
            if t not in cleaned:
                cleaned.append(t)
        if not cleaned:
            raise ValueError("tfs must not be empty")
        return cleaned


# --------------------
# Helpers
# --------------------
def norm_symbol(sym: str) -> str:
    s = sym.replace(" ", "")
    if s.upper() == "XAUUSD":
        return "XAU/USD"
    return sym

def td_fetch_series(symbol: str, tf: str, size: int = 300) -> List[Dict[str, Any]]:
    if not TWELVEDATA_API_KEY:
        raise HTTPException(500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": ALLOWED_TFS[tf],  # map to twelvedata interval
        "outputsize": size,
        "order": "desc",
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=25)
        data = r.json()
    except Exception:
        raise HTTPException(502, detail="Upstream not JSON")

    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(502, detail=f"TwelveData: {data.get('message','error')}")

    values = data.get("values")
    if not values:
        raise HTTPException(502, detail="No bars from TwelveData")

    # values is latest-first. Normalize numeric types.
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
            continue

    if not bars:
        raise HTTPException(502, detail="Cannot parse bars")
    return bars  # latest-first


def last_closed(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    # TwelveData returns closed bars latest-first; use index 0.
    return bars[0]


# --- Simple SR detection (swing based clustering) ---
def detect_support_resistance(
    bars: List[Dict[str, Any]],
    swing_lookback: int = 5,
    cluster_tol: float = 0.25,
    top_n: int = 2
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Find swing highs/lows and then cluster them to produce compact S/R levels.
    - swing_lookback: number of bars to check around pivot (2 on each side by default if 5)
    - cluster_tol: points tolerance to merge nearby levels
    - top_n: limit number of S and R levels
    """
    if len(bars) < max(20, swing_lookback + 3):
        return {"resistance": [], "support": []}

    # work with oldest-first for pivot check
    seq = list(reversed(bars))  # oldest -> newest

    piv_highs: List[float] = []
    piv_lows: List[float] = []

    k = swing_lookback // 2
    for i in range(k, len(seq) - k):
        window = seq[i - k: i + k + 1]
        high_vals = [b["high"] for b in window]
        low_vals = [b["low"] for b in window]
        c = seq[i]
        if c["high"] == max(high_vals):
            piv_highs.append(c["high"])
        if c["low"] == min(low_vals):
            piv_lows.append(c["low"])

    def cluster(levels: List[float], tol: float, reverse: bool) -> List[Dict[str, Any]]:
        # merge nearby levels; touches = count inside cluster
        levels = sorted(levels, reverse=reverse)
        clusters: List[Dict[str, Any]] = []
        for lv in levels:
            placed = False
            for c in clusters:
                if abs(c["price"] - lv) <= tol:
                    # running average for center
                    c["price"] = (c["price"] * c["touches"] + lv) / (c["touches"] + 1)
                    c["touches"] += 1
                    placed = True
                    break
            if not placed:
                clusters.append({"price": lv, "touches": 1})
        # resort by importance (touches) then by price desc for R / asc for S
        clusters.sort(key=lambda x: (-x["touches"], -x["price"] if reverse else x["price"]))
        return clusters[:top_n]

    rs = cluster(piv_highs, cluster_tol, reverse=True)
    ss = cluster(piv_lows, cluster_tol, reverse=False)

    # round to 2 decimals for XAU ticks-like presentation
    for c in rs + ss:
        c["price"] = round(c["price"], 2)
    return {"resistance": rs, "support": ss}


# --- Very light Order Block detection ---
def detect_order_blocks(bars: List[Dict[str, Any]], min_impulse_points: float = 6.0) -> List[Dict[str, Any]]:
    """
    Simple OB finder:
      - Bullish OB: bearish base candle followed by a strong bullish impulse (close - open >= min_impulse_points).
      - Bearish OB: bullish base candle followed by a strong bearish impulse (open - close >= min_impulse_points).
    Returns the most recent OB (bullish/ bearish) with its price range.
    """
    if len(bars) < 5:
        return []

    # latest-first; check from newest -> older
    for i in range(1, min(60, len(bars) - 1)):
        base = bars[i]     # prior bar
        imp = bars[i - 1]  # impulse bar (newer)

        bull_imp = (imp["close"] - imp["open"]) >= min_impulse_points
        bear_imp = (imp["open"] - imp["close"]) >= min_impulse_points

        if base["close"] < base["open"] and bull_imp:
            # Bullish OB zone: base low .. base open (conservative)
            low = min(base["low"], base["open"])
            high = max(base["low"], base["open"])
            return [{"side": "Bullish", "range": [round(low, 2), round(high, 2)]}]

        if base["close"] > base["open"] and bear_imp:
            # Bearish OB zone: base open .. base high
            low = min(base["open"], base["high"])
            high = max(base["open"], base["high"])
            return [{"side": "Bearish", "range": [round(low, 2), round(high, 2)]}]

    return []


def build_tf_snapshot(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    sr = detect_support_resistance(bars, swing_lookback=5, cluster_tol=0.25, top_n=2)
    obs = detect_order_blocks(bars, min_impulse_points=6.0)
    last = last_closed(bars)
    out = {
        "last_bar": {
            "dt": last["dt"],
            "open": last["open"],
            "high": last["high"],
            "low": last["low"],
            "close": last["close"],
        },
        "resistance": sr["resistance"],
        "support": sr["support"],
        "order_blocks": obs,
    }
    return out


# --------------------
# Routes
# --------------------
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

@app.post("/structure")
def structure(req: StructureRequest):
    symbol = norm_symbol(req.symbol)
    results: Dict[str, Any] = {}
    try:
        for tf in req.tfs:
            bars = td_fetch_series(symbol, tf, size=300)
            results[tf] = build_tf_snapshot(bars)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=f"structure error: {e}")

    return {
        "status": "OK",
        "symbol": symbol,
        "results": results,
    }
