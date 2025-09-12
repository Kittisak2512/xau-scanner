# main.py
import os
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, timezone
import traceback

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_VERSION = "2025-09-12.dynamic-rs-4"  # ใช้เช็คว่าเป็นไฟล์ใหม่จริง

# =========================
# Config
# =========================
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
TD_BASE = "https://api.twelvedata.com"

_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOW_ORIGINS = ["*"] if (not _ALLOWED or _ALLOWED == "*") else [
    o.strip() for o in _ALLOWED.split(",") if o.strip()
]

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
    symbol: str = Field(..., description="e.g. XAUUSD or XAU/USD")
    tf: Optional[str] = Field(None, description="Single timeframe (optional)")
    tfs: Optional[List[str]] = Field(None, description="List of timeframes (optional)")

# =========================
# Helpers
# =========================
def normalize_symbol(sym: str) -> str:
    s = (sym or "").upper().replace(" ", "")
    if "/" in s:
        return s
    if len(s) == 6:
        return f"{s[:3]}/{s[3:]}"
    return s

def tf_to_interval(tf: str) -> str:
    t = (tf or "").upper()
    m = {"M5":"5min","M15":"15min","M30":"30min","H1":"1h","H4":"4h","D1":"1day"}
    if t not in m:
        raise HTTPException(status_code=400, detail=f"Unsupported timeframe: {tf}")
    return m[t]

@dataclass
class Candle:
    dt: str; open: float; high: float; low: float; close: float

def fetch_bars(symbol: str, tf: str, size: int = 300) -> List[Candle]:
    if not TD_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")
    url = f"{TD_BASE}/time_series"
    params = {"symbol":symbol, "interval":tf_to_interval(tf), "outputsize":size,
              "order":"desc", "timezone":"UTC", "apikey":TD_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=25)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Upstream connection error.")
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON.")
    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(status_code=502, detail=data.get("message", "TwelveData error"))
    vals = data.get("values")
    if not vals:
        raise HTTPException(status_code=502, detail="No data from TwelveData")
    bars: List[Candle] = []
    for v in vals:
        try:
            bars.append(Candle(dt=v["datetime"], open=float(v["open"]),
                               high=float(v["high"]), low=float(v["low"]), close=float(v["close"])))
        except Exception:
            continue
    if not bars:
        raise HTTPException(status_code=502, detail="Cannot parse bars")
    return bars  # latest first (closed)

def last_closed(bars: List[Candle]) -> Candle:
    return bars[0]

def detect_swings(bars: List[Candle], left: int = 2, right: int = 2) -> Dict[str, List[float]]:
    highs: List[float] = []; lows: List[float] = []
    seq = list(reversed(bars))  # old -> new
    n = len(seq)
    for i in range(left, n - right):
        h = seq[i].high; l = seq[i].low
        if all(h > seq[i-k].high for k in range(1,left+1)) and all(h > seq[i+k].high for k in range(1,right+1)):
            highs.append(h)
        if all(l < seq[i-k].low for k in range(1,left+1)) and all(l < seq[i+k].low for k in range(1,right+1)):
            lows.append(l)
    highs.sort(reverse=True); lows.sort(reverse=True)
    return {"swing_highs": highs, "swing_lows": lows}

def nearest_levels_above_below(current: float, highs: List[float], lows: List[float]) -> Dict[str, Optional[float]]:
    above = [h for h in highs if h > current]
    below = [l for l in lows if l < current]
    res = min(above, key=lambda x: x-current) if above else (max(highs) if highs else None)
    sup = max(below, key=lambda x: current-x) if below else (min(lows) if lows else None)
    return {"resistance": res, "support": sup}

def find_order_blocks(bars: List[Candle], window: int = 40, body_ratio: float = 0.6) -> List[Dict[str, Any]]:
    seq = list(reversed(bars))[:window]  # old -> new
    out: List[Dict[str, Any]] = []
    if len(seq) < 5:
        return out
    def body(c: Candle) -> float: return abs(c.close - c.open)
    for i in range(3, len(seq) - 1):
        zone = seq[i-3:i]
        z_high = max(c.high for c in zone); z_low = min(c.low for c in zone)
        brk = seq[i]; rng = brk.high - brk.low
        if rng <= 0: continue
        if body(brk) >= body_ratio * rng:
            if brk.close > z_high:
                out.append({"type":"bullish","range":[round(z_low,2), round(z_high,2)]})
            elif brk.close < z_low:
                out.append({"type":"bearish","range":[round(z_low,2), round(z_high,2)]})
    return out[-2:]

def fmt2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 2)

# =========================
# Core (SAFE + DEBUG TRACE)
# =========================
def build_structure(symbol_raw: str, tfs: List[str]) -> Dict[str, Any]:
    symbol = normalize_symbol(symbol_raw)
    result: Dict[str, Any] = {"symbol": symbol, "result": {}}

    for tf in tfs:
        tf_key = (tf or "").upper() or "H1"
        try:
            bars = fetch_bars(symbol, tf_key, size=300)
            last = last_closed(bars)
            swings = detect_swings(bars, left=2, right=2)
            current = last.close

            # ---- defaults (กันพลาดแน่นอน) ----
            resistance: Optional[float] = None
            support: Optional[float] = None
            order_blocks: List[Dict[str, Any]] = []

            lv = nearest_levels_above_below(current, swings["swing_highs"], swings["swing_lows"])
            resistance = fmt2(lv.get("resistance"))
            support = fmt2(lv.get("support"))

            if resistance is not None and resistance <= current:
                resistance = fmt2(max(current + 0.01, last.high))
            if support is not None and support >= current:
                support = fmt2(min(current - 0.01, last.low))

            # หา OB แบบปลอดภัย
            try:
                tmp = find_order_blocks(bars, window=40)
                order_blocks = tmp if isinstance(tmp, list) else []
            except Exception:
                order_blocks = []

            result["result"][tf_key] = {
                "last_bar": {
                    "dt": last.dt,
                    "open": fmt2(last.open),
                    "high": fmt2(last.high),
                    "low": fmt2(last.low),
                    "close": fmt2(last.close),
                },
                "resistance": resistance,
                "support": support,
                "order_blocks": order_blocks,   # ← มีค่าชัวร์
            }

        except Exception as e:
            # แนบ traceback 3 บรรทัด เพื่อรู้ว่าพังตรงไหน
            tb = traceback.format_exc().splitlines()[-6:]
            result["result"][tf_key] = {"error": str(e), "error_trace": tb, "version": APP_VERSION}

    return result

# =========================
# Routes
# =========================
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat(), "version": APP_VERSION}

# ---- POST (JSON) ----
@app.post("/structure")
def structure_post(req: StructureRequest):
    try:
        allowed = {"M5","M15","M30","H1","H4","D1"}
        raw = []
        if req.tfs: raw.extend(req.tfs)
        if req.tf: raw.append(req.tf)
        tfs = [t.upper() for t in raw if t and t.upper() in allowed] or ["H1"]
        return build_structure(req.symbol, tfs)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---- GET (query) ----
@app.get("/structure")
def structure_get(
    symbol: str = Query(..., description="e.g. XAUUSD or XAU/USD"),
    tf: Optional[str] = Query(None, description="Single TF, e.g. H1"),
    tfs: Optional[str] = Query(None, description="Comma-separated, e.g. M5,M15,H1"),
):
    try:
        allowed = {"M5","M15","M30","H1","H4","D1"}
        raw: List[str] = []
        if tfs: raw.extend([x.strip() for x in tfs.split(",") if x.strip()])
        if tf: raw.append(tf)
        out = [t.upper() for t in raw if t and t.upper() in allowed] or ["H1"]
        return build_structure(symbol, out)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
