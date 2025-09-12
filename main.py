# main.py — XAU Structure Scanner (No Entry Signal)
import os
from typing import List, Dict, Any, Tuple
from datetime import datetime

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-12.STRUCTURE.2"  # +M30, D1

# =======================
# ENV / Config
# =======================
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOW_ORIGINS = ["*"] if _ALLOWED in ("", "*") else [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# =======================
# FastAPI
# =======================
app = FastAPI(title="xau-structure-scanner", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =======================
# Models
# =======================
class Candle(BaseModel):
    dt: str
    open: float
    high: float
    low: float
    close: float

class StructureRequest(BaseModel):
    symbol: str = Field(..., examples=["XAU/USD"])
    tfs: List[str] = Field(default_factory=lambda: ["M5", "M15", "M30", "H1", "H4", "D1"])

    @field_validator("tfs")
    @classmethod
    def _vtfs(cls, v: List[str]) -> List[str]:
        allowed = {"M5", "M15", "M30", "H1", "H4", "D1"}
        vv = [x.upper() for x in v]
        for t in vv:
            if t not in allowed:
                raise ValueError(f"Unsupported TF: {t}")
        return vv

# =======================
# TwelveData utils
# =======================
TF_MAP = {
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1day",
}

def td_interval(tf: str) -> str:
    t = tf.upper()
    if t not in TF_MAP:
        raise ValueError(f"Unsupported TF: {tf}")
    return TF_MAP[t]

def fetch_series(symbol: str, tf: str, size: int) -> List[Candle]:
    if not TWELVEDATA_API_KEY:
        raise HTTPException(500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",      # latest first (closed bars)
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    r = requests.get(url, params=params, timeout=20)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(502, detail="Upstream returned non-JSON")

    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(502, detail=str(data.get("message", "API error")))
    values = data.get("values")
    if not values:
        raise HTTPException(502, detail="No data from TwelveData")

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
        raise HTTPException(502, detail="Cannot parse candles")
    return out  # latest first

# =======================
# Structure detectors
# =======================
def find_pivots(bars: List[Candle], left: int = 2, right: int = 2) -> Dict[str, List[Dict[str, Any]]]:
    """หา pivot highs/lows แบบ fractal (แปลงลำดับให้เป็น เก่า->ใหม่ ก่อนคำนวณ)"""
    highs, lows = [], []
    seq = list(reversed(bars))  # เก่า -> ใหม่
    n = len(seq)
    for i in range(left, n - right):
        win = seq[i - left:i + right + 1]
        c = seq[i]
        if all(c.high >= w.high for w in win) and any(c.high > w.high for w in win if w != c):
            highs.append({"i": i, "dt": c.dt, "price": c.high})
        if all(c.low <= w.low for w in win) and any(c.low < w.low for w in win if w != c):
            lows.append({"i": i, "dt": c.dt, "price": c.low})
    return {"highs": highs, "lows": lows}

def cluster_levels(points: List[float], band: float, min_hits: int = 3, max_levels: int = 12) -> List[Dict[str, Any]]:
    """รวมระดับราคาที่อยู่ใกล้กัน (band หน่วยจุด) → สร้าง R/S ที่มีการแตะซ้ำ"""
    if not points:
        return []
    points = sorted(points)
    clusters = []
    cur = [points[0]]
    for p in points[1:]:
        if abs(p - cur[-1]) <= band:
            cur.append(p)
        else:
            clusters.append(cur)
            cur = [p]
    clusters.append(cur)

    lvls = []
    for c in clusters:
        if len(c) >= min_hits:
            lvls.append({"price": round(sum(c) / len(c), 2), "hits": len(c)})
    lvls.sort(key=lambda x: (-x["hits"], x["price"]))
    return lvls[:max_levels]

def fit_line(p1: Tuple[int, float], p2: Tuple[int, float]) -> Tuple[float, float]:
    """สมการเส้นตรง y = a*x + b"""
    (x1, y1), (x2, y2) = p1, p2
    if x2 == x1:
        x2 += 1e-6
    a = (y2 - y1) / (x2 - x1)
    b = y1 - a * x1
    return a, b

def line_error(a: float, b: float, pts: List[Tuple[int, float]]) -> List[float]:
    return [abs((a * x + b) - y) for x, y in pts]

def detect_trendlines(pivots: List[Dict[str, Any]], tol: float, min_touches: int = 3, max_lines: int = 3) -> List[Dict[str, Any]]:
    """หา trendline จากสวิง (ต้องมีจุดสัมผัส ≥ min_touches และ error ≤ tol)"""
    if len(pivots) < min_touches:
        return []
    pts = [(p["i"], p["price"]) for p in pivots]
    n = len(pts)
    lines = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = fit_line(pts[i], pts[j])
            errs = line_error(a, b, pts)
            inliers = [k for k, e in enumerate(errs) if e <= tol]
            if len(inliers) >= min_touches:
                xs = [pts[k][0] for k in inliers]
                ys = [pts[k][1] for k in inliers]
                line = {
                    "a": a, "b": b,
                    "touches": len(inliers),
                    "x_min": int(min(xs)), "x_max": int(max(xs)),
                    "y_min": round(min(ys), 2), "y_max": round(max(ys), 2),
                }
                dup = any(abs(a - L["a"]) < 1e-5 and abs(b - L["b"]) < 20 for L in lines)
                if not dup:
                    lines.append(line)
    lines.sort(key=lambda L: (-L["touches"], L["x_max"] - L["x_min"]))
    return lines[:max_lines]

def detect_order_blocks(bars: List[Candle], piv: Dict[str, List[Dict[str, Any]]], lookback: int = 160) -> List[Dict[str, Any]]:
    """
    OB แบบคัดเฉพาะจังหวะที่ชัด (ก่อน BoS):
    - BoS ลง: close ทะลุ swing low สำคัญ → last up candle ก่อนหน้า = Bearish OB
    - BoS ขึ้น: close ทะลุ swing high สำคัญ → last down candle ก่อนหน้า = Bullish OB
    """
    seq = list(reversed(bars))[:lookback]  # เก่า->ใหม่
    highs = piv["highs"]; lows = piv["lows"]
    obs = []

    key_highs = sorted(highs, key=lambda x: x["i"], reverse=True)[:6]
    key_lows  = sorted(lows,  key=lambda x: x["i"], reverse=True)[:6]

    # BoS ลง → Bearish OB
    for kl in key_lows:
        lvl = kl["price"]
        bos_idx = next((i for i, c in enumerate(seq) if c.close < lvl), None)
        if bos_idx is not None:
            last_up = next((j for j in range(bos_idx - 1, max(bos_idx - 40, -1), -1) if seq[j].close > seq[j].open), None)
            if last_up is not None:
                oc = seq[last_up]
                obs.append({
                    "type": "bearish",
                    "dt": oc.dt,
                    "zone": [round(min(oc.open, oc.close), 2), round(max(oc.open, oc.close), 2)]
                })
                break

    # BoS ขึ้น → Bullish OB
    for kh in key_highs:
        lvl = kh["price"]
        bos_idx = next((i for i, c in enumerate(seq) if c.close > lvl), None)
        if bos_idx is not None:
            last_dn = next((j for j in range(bos_idx - 1, max(bos_idx - 40, -1), -1) if seq[j].close < seq[j].open), None)
            if last_dn is not None:
                oc = seq[last_dn]
                obs.append({
                    "type": "bullish",
                    "dt": oc.dt,
                    "zone": [round(min(oc.open, oc.close), 2), round(max(oc.open, oc.close), 2)]
                })
                break

    return obs

def band_tol_for_tf(tf: str) -> Tuple[float, float]:
    """
    ให้ band (สำหรับ cluster R/S) และ tol (สำหรับ trendline) ต่อ TF
    ปรับตามความผันผวน: TF ใหญ่ band/tol กว้างขึ้น
    """
    tf = tf.upper()
    if tf == "M5":   return 30.0, 40.0
    if tf == "M15":  return 30.0, 40.0
    if tf == "M30":  return 40.0, 55.0
    if tf == "H1":   return 60.0, 70.0
    if tf == "H4":   return 120.0, 120.0
    if tf == "D1":   return 180.0, 200.0
    return 60.0, 80.0

def analyze_structure_for_tf(symbol: str, tf: str) -> Dict[str, Any]:
    """
    คืนผลโครงสร้างต่อ TF:
    - levels.resistance / levels.support : [{price,hits}]
    - trendlines.down/up : [{a,b,touches,x_min,x_max,y_min,y_max}]
    - order_blocks : [{type,dt,zone:[low,high]}]
    - meta.last_bar : แท่งล่าสุด
    """
    # ขยายจำนวนแท่งตาม TF ให้ D1/H4 มีข้อมูลมากขึ้น
    size = 300 if tf in ("M5","M15","M30","H1") else 400
    bars = fetch_series(symbol, tf, size)
    piv = find_pivots(bars, left=2, right=2)

    band, tol = band_tol_for_tf(tf)

    levels_high = cluster_levels([p["price"] for p in piv["highs"]], band=band)
    levels_low  = cluster_levels([p["price"] for p in piv["lows"]],  band=band)

    tl_down = detect_trendlines(piv["highs"], tol=tol)  # แนวต้าน
    tl_up   = detect_trendlines(piv["lows"],  tol=tol)  # แนวรับ

    obs = detect_order_blocks(bars, piv, lookback=200 if tf in ("H4","D1") else 160)

    return {
        "tf": tf,
        "levels": {"resistance": levels_high, "support": levels_low},
        "trendlines": {"down": tl_down, "up": tl_up},
        "order_blocks": obs,
        "meta": {"last_bar": bars[0].model_dump()},
    }

# =======================
# Routes
# =======================
@app.get("/")
def root():
    return {"app": "xau-structure-scanner", "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.utcnow().isoformat() + "Z"}

@app.post("/analyze-structure")
def analyze_structure(req: StructureRequest):
    try:
        out = {"status": "OK", "symbol": req.symbol, "result": []}
        for tf in req.tfs:
            out["result"].append(analyze_structure_for_tf(req.symbol, tf))
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
