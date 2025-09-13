# main.py
import os
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-13.zones-ob-1"

# =========================
# Config
# =========================
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOW_ORIGINS = ["*"] if _ALLOWED in ("", "*") else [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# =========================
# App
# =========================
app = FastAPI(title="xau-scanner", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Models
# =========================
class StructureRequest(BaseModel):
    symbol: str = Field(..., examples=["XAUUSD", "XAU/USD"])
    tfs: List[str] = Field(..., description="List of TFs", examples=[["M5", "M15", "M30", "H1", "H4", "D1"]])

    @field_validator("tfs")
    @classmethod
    def v_tfs(cls, v: List[str]) -> List[str]:
        ok = {"M5", "M15", "M30", "H1", "H4", "D1"}
        out = []
        for tf in v:
            u = tf.upper()
            if u not in ok:
                raise ValueError(f"Unsupported TF: {tf}")
            out.append(u)
        # dedup (preserve order)
        seen = set()
        dedup = []
        for x in out:
            if x not in seen:
                seen.add(x)
                dedup.append(x)
        return dedup


@dataclass
class Candle:
    dt: str
    open: float
    high: float
    low: float
    close: float


# =========================
# Utilities
# =========================
def normalize_symbol(sym: str) -> str:
    """
    Make 'XAUUSD' → 'XAU/USD', 'XAU / USD' → 'XAU/USD'
    If already includes '/', keep as is.
    """
    s = sym.strip().upper().replace(" ", "")
    if "/" in s:
        return s
    # common 6-letter FX metals/forex
    if len(s) == 6:
        return f"{s[:3]}/{s[3:]}"
    return s


def tf_to_td(tf: str) -> str:
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
        raise ValueError(f"Unsupported TF: {tf}")
    return mapping[m]


def fetch_series(symbol: str, interval: str, size: int = 320) -> List[Candle]:
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": size,
        "order": "desc",      # latest first
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    r = requests.get(url, params=params, timeout=25)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON")

    if "status" in data and data["status"] == "error":
        raise HTTPException(status_code=502, detail=str(data.get("message", "API error")))
    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData")

    bars: List[Candle] = []
    for v in values:
        try:
            bars.append(
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

    if len(bars) < 10:
        raise HTTPException(status_code=502, detail="Too few bars")
    return bars  # latest first


# =========================
# Swings & Zones
# =========================
def find_swings(bars: List[Candle], lookback: int = 220, k: int = 3) -> Dict[str, List[float]]:
    """
    Simple pivot detection:
      - pivot high at i if high[i] is the max in [i-k, i+k]
      - pivot low  at i if low[i]  is the min in [i-k, i+k]
    We process the most recent 'lookback' portion (old→new).
    """
    seq = list(reversed(bars[: max(lookback, 60)]))  # old -> new
    highs: List[float] = []
    lows: List[float] = []
    n = len(seq)
    if n == 0:
        return {"highs": [], "lows": []}

    for i in range(n):
        L = max(0, i - k)
        R = min(n - 1, i + k)
        window = seq[L : R + 1]
        hmax = max(c.high for c in window)
        hmin = min(c.low for c in window)
        if seq[i].high >= hmax:
            highs.append(round(seq[i].high, 2))
        if seq[i].low <= hmin:
            lows.append(round(seq[i].low, 2))
    return {"highs": highs, "lows": lows}


def cluster_levels_to_zones(levels: List[float], band: float = 8.0, min_width: float = 4.0) -> List[Tuple[float, float]]:
    """
    Group nearby levels into price 'zones'.
    """
    if not levels:
        return []
    lv = sorted(levels)
    zones: List[Tuple[float, float]] = []
    lo = hi = lv[0]
    for x in lv[1:]:
        if x - hi <= band:
            hi = x
        else:
            if hi - lo < min_width:
                mid = 0.5 * (lo + hi)
                lo, hi = mid - min_width / 2.0, mid + min_width / 2.0
            zones.append((round(lo, 2), round(hi, 2)))
            lo = hi = x
    # last
    if hi - lo < min_width:
        mid = 0.5 * (lo + hi)
        lo, hi = mid - min_width / 2.0, mid + min_width / 2.0
    zones.append((round(lo, 2), round(hi, 2)))
    return zones


def nearest_zone_above(zones: List[Tuple[float, float]], price: float) -> Optional[Tuple[float, float]]:
    cand: List[Tuple[float, Tuple[float, float]]] = []
    for lo, hi in zones:
        zlo, zhi = min(lo, hi), max(lo, hi)
        if zlo > price:  # fully above
            mid = 0.5 * (zlo + zhi)
            cand.append((abs(mid - price), (zlo, zhi)))
    return min(cand, key=lambda x: x[0])[1] if cand else None


def nearest_zone_below(zones: List[Tuple[float, float]], price: float) -> Optional[Tuple[float, float]]:
    cand: List[Tuple[float, Tuple[float, float]]] = []
    for lo, hi in zones:
        zlo, zhi = min(lo, hi), max(lo, hi)
        if zhi < price:  # fully below
            mid = 0.5 * (zlo + zhi)
            cand.append((abs(mid - price), (zlo, zhi)))
    return min(cand, key=lambda x: x[0])[1] if cand else None


# =========================
# Order Blocks (เรียบง่ายแต่มีช่วงราคา)
# =========================
def detect_order_blocks(bars: List[Candle], max_blocks: int = 3) -> List[Dict[str, float]]:
    """
    Very simple OB detection:
      - Bullish OB: last bearish candle before an 'up impulse' (next 2 bars making higher highs/closes)
      - Bearish OB: last bullish candle before a 'down impulse'
      Zone = [min(open, close), max(open, close)] of the base candle.
    Returns most-recent first, up to max_blocks.
    """
    seq = list(reversed(bars[: 180]))  # old -> new
    if len(seq) < 5:
        return []

    obs: List[Tuple[str, float, float, int]] = []  # (type, low, high, idx)

    for i in range(2, len(seq) - 2):
        c0 = seq[i]     # candidate base
        c1 = seq[i + 1]
        c2 = seq[i + 2]

        # up impulse
        up_impulse = (c1.high > c0.high and c2.close > c1.close and c2.close > c0.close)
        # down impulse
        dn_impulse = (c1.low < c0.low and c2.close < c1.close and c2.close < c0.close)

        # bearish base (red candle) before up move -> bullish OB
        if c0.close < c0.open and up_impulse:
            lo = round(min(c0.open, c0.close), 2)
            hi = round(max(c0.open, c0.close), 2)
            obs.append(("bullish", lo, hi, i))

        # bullish base (green candle) before down move -> bearish OB
        if c0.close > c0.open and dn_impulse:
            lo = round(min(c0.open, c0.close), 2)
            hi = round(max(c0.open, c0.close), 2)
            obs.append(("bearish", lo, hi, i))

    # keep most recent (bigger index i is newer)
    obs.sort(key=lambda x: x[3], reverse=True)
    out: List[Dict[str, float]] = []
    for t, lo, hi, _ in obs[:max_blocks]:
        if hi - lo >= 0.5:  # drop tiny zones
            out.append({"type": t, "low": lo, "high": hi})
    return out


# =========================
# TF block
# =========================
def build_tf_block(symbol: str, tf: str, lookback: int = 240) -> Dict[str, Any]:
    """
    For a TF:
      - compute swings & cluster into zones
      - choose resistance_zone (above price) from swing highs
      - choose support_zone    (below price) from swing lows
      - enforce min_gap to avoid overlapping
      - detect order blocks
    """
    bars = fetch_series(symbol, tf_to_td(tf), size=max(lookback + 80, 320))
    last = bars[0]
    price = last.close

    swings = find_swings(bars, lookback=lookback, k=3)
    swing_highs = swings.get("highs", [])
    swing_lows = swings.get("lows", [])

    # tune these two to widen/narrow zones
    high_zones = cluster_levels_to_zones(swing_highs, band=8.0, min_width=4.0)
    low_zones = cluster_levels_to_zones(swing_lows, band=8.0, min_width=4.0)

    res_zone = nearest_zone_above(high_zones, price)
    sup_zone = nearest_zone_below(low_zones, price)

    # enforce minimal gap
    min_gap = 6.0
    if res_zone and sup_zone:
        r_lo, r_hi = res_zone
        s_lo, s_hi = sup_zone
        if r_lo - s_hi < min_gap:
            if (r_lo - price) > (price - s_hi):
                shift = (min_gap - (r_lo - s_hi))
                res_zone = (round(r_lo + shift, 2), round(r_hi + shift, 2))
            else:
                shift = (min_gap - (r_lo - s_hi))
                sup_zone = (round(s_lo - shift, 2), round(s_hi - shift, 2))

    resistance = round(sum(res_zone) / 2.0, 2) if res_zone else None
    support = round(sum(sup_zone) / 2.0, 2) if sup_zone else None

    order_blocks = detect_order_blocks(bars)

    return {
        "tf": tf,
        "last_bar": {
            "dt": last.dt,
            "open": last.open,
            "high": last.high,
            "low": last.low,
            "close": last.close,
        },
        "resistance": resistance,
        "support": support,
        "resistance_zone": res_zone,  # (low, high) or null
        "support_zone": sup_zone,     # (low, high) or null
        "order_blocks": order_blocks, # [{type,low,high}, ...]
    }


# =========================
# Routes
# =========================
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


@app.get("/health")
def health():
    import datetime as dt
    return {"ok": True, "ts": dt.datetime.utcnow().isoformat() + "Z"}


@app.post("/structure")
def structure(req: StructureRequest):
    symbol = normalize_symbol(req.symbol)
    try:
        results: List[Dict[str, Any]] = []
        for tf in req.tfs:
            block = build_tf_block(symbol, tf)
            results.append(block)
        return {
            "status": "OK",
            "symbol": symbol,
            "results": results,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
