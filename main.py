# main.py
import os
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import requests
import math

APP_VERSION = "2025-09-12.dynamic-rs-1"

# =========================
# Config
# =========================
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
TD_BASE = "https://api.twelvedata.com"

_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
ALLOW_ORIGINS = ["*"] if not _ALLOWED or _ALLOWED == "*" else [o.strip() for o in _ALLOWED.split(",") if o.strip()]

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
    tfs: List[str] = Field(..., description="List of timeframes, e.g. ['M5','M15','M30','H1','H4','D1']")


# =========================
# Helpers
# =========================
def normalize_symbol(sym: str) -> str:
    """Auto convert XAUUSD -> XAU/USD (รองรับ 6 ตัวอักษรทั่วไป)"""
    s = sym.upper().replace(" ", "")
    if "/" in s:
        return s
    if len(s) == 6:
        base, quote = s[:3], s[3:]
        return f"{base}/{quote}"
    return s


def tf_to_interval(tf: str) -> str:
    t = tf.upper()
    m = {
        "M5": "5min",
        "M15": "15min",
        "M30": "30min",
        "H1": "1h",
        "H4": "4h",
        "D1": "1day",
    }
    if t not in m:
        raise HTTPException(status_code=400, detail=f"Unsupported timeframe: {tf}")
    return m[t]


@dataclass
class Candle:
    dt: str
    open: float
    high: float
    low: float
    close: float


def fetch_bars(symbol: str, tf: str, size: int = 300) -> List[Candle]:
    if not TD_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")
    url = f"{TD_BASE}/time_series"
    params = {
        "symbol": symbol,
        "interval": tf_to_interval(tf),
        "outputsize": size,
        "order": "desc",          # latest first
        "timezone": "UTC",
        "apikey": TD_API_KEY,
    }
    r = requests.get(url, params=params, timeout=25)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON.")

    if "status" in data and data["status"] == "error":
        raise HTTPException(status_code=502, detail=data.get("message", "TwelveData error"))

    vals = data.get("values")
    if not vals:
        raise HTTPException(status_code=502, detail="No data from TwelveData")

    bars: List[Candle] = []
    for v in vals:
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

    if not bars:
        raise HTTPException(status_code=502, detail="Cannot parse bars")
    return bars  # latest first


def last_closed(bars: List[Candle]) -> Candle:
    # TwelveData with order=desc already returns closed bars; index 0 is latest closed
    return bars[0]


def detect_swings(bars: List[Candle], left: int = 2, right: int = 2) -> Dict[str, List[float]]:
    """
    หา Swing High/Low แบบเรียบง่าย:
    - swing high: high[i] > high[i-1..i-left] และ > high[i+1..i+right]
    - swing low : low[i]  < low[i-1..i-left]  และ < low[i+1..i+right]
    """
    highs: List[float] = []
    lows: List[float] = []
    n = len(bars)
    # ใช้ข้อมูลย้อนกลับ (เพราะ bars เป็นล่าสุดก่อน) แปลงเป็นเก่าก่อนไปใหม่หลังเพื่อดูเพื่อนบ้านง่าย
    seq = list(reversed(bars))  # old -> new
    for i in range(left, n - right):
        h = seq[i].high
        l = seq[i].low
        is_sh = all(h > seq[i - k].high for k in range(1, left + 1)) and all(h > seq[i + k].high for k in range(1, right + 1))
        is_sl = all(l < seq[i - k].low for k in range(1, left + 1)) and all(l < seq[i + k].low for k in range(1, right + 1))
        if is_sh:
            highs.append(h)
        if is_sl:
            lows.append(l)
    # เรียงจากใหม่ไปเก่าเพื่อเลือกใกล้ราคา
    highs = sorted(highs, reverse=True)
    lows = sorted(lows, reverse=True)
    return {"swing_highs": highs, "swing_lows": lows}


def nearest_levels_above_below(current: float, highs: List[float], lows: List[float]) -> Dict[str, Optional[float]]:
    """
    เลือก Resistance เป็น swing high ที่ 'สูงกว่า' current และ 'ใกล้ที่สุด'
    เลือก Support   เป็น swing low  ที่ 'ต่ำกว่า' current และ 'ใกล้ที่สุด'
    ถ้าไม่มีฝั่งใดฝั่งหนึ่ง ให้ fallback เป็น max(highs) / min(lows) ตามทิศทาง เพื่อไม่คืน None
    """
    above = [h for h in highs if h > current]
    below = [l for l in lows if l < current]

    res = min(above, key=lambda x: x - current) if above else (max(highs) if highs else None)
    sup = max(below, key=lambda x: current - x) if below else (min(lows) if lows else None)

    return {"resistance": res, "support": sup}


def find_order_blocks(bars: List[Candle], window: int = 30, body_ratio: float = 0.6) -> List[Dict[str, Any]]:
    """
    หาโซน Order Block แบบเบา ๆ:
      - มองหาระยะสะสมสั้น ๆ ต่อด้วยแท่งทะลุที่มี body ใหญ่ (>= body_ratio ของ high-low)
      - คืนช่วงราคา [low, high] ของโซนสะสมสุดท้ายก่อนเบรก
    """
    seq = list(reversed(bars))[:window]  # old -> new, จำกัดหน้าต่าง
    out: List[Dict[str, Any]] = []
    if len(seq) < 5:
        return out

    def body(c: Candle) -> float:
        return abs(c.close - c.open)

    for i in range(3, len(seq) - 1):
        # โซนสะสม 3 แท่งก่อนหน้า
        zone = seq[i - 3 : i]
        z_high = max(c.high for c in zone)
        z_low = min(c.low for c in zone)
        brk = seq[i]
        rng = brk.high - brk.low
        if rng <= 0:
            continue
        if body(brk) >= body_ratio * rng:
            # ทะลุขึ้น/ลงจากโซน
            if brk.close > z_high:
                out.append({"type": "bullish", "range": [round(z_low, 2), round(z_high, 2)]})
            elif brk.close < z_low:
                out.append({"type": "bearish", "range": [round(z_low, 2), round(z_high, 2)]})
    # คืนล่าสุด 2 ก้อนพอ
    return out[-2:]


def fmt2(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 2)


# =========================
# Core
# =========================
def build_structure(symbol_raw: str, tfs: List[str]) -> Dict[str, Any]:
    symbol = normalize_symbol(symbol_raw)
    result: Dict[str, Any] = {"symbol": symbol, "result": {}}

    for tf in tfs:
        bars = fetch_bars(symbol, tf, size=300)
        last = last_closed(bars)
        swings = detect_swings(bars, left=2, right=2)
        current = last.close

        lv = nearest_levels_above_below(current, swings["swing_highs"], swings["swing_lows"])
        resistance = fmt2(lv["resistance"])
        support = fmt2(lv["support"])

        # ปลอดภัยอีกชั้น: ถ้าโชคไม่ดี resistance ≤ current ให้ดันขึ้นเล็กน้อยจาก high ล่าสุด
        if resistance is not None and resistance <= current:
            resistance = fmt2(max(current + 0.01, last.high))
        # ถ้า support ≥ current ให้ดันลงเล็กน้อยจาก low ล่าสุด
        if support is not None and support >= current:
            support = fmt2(min(current - 0.01, last.low))

        obs = find_order_blocks(bars, window=40)

        result["result"][tf.upper()] = {
            "last_bar": {
                "dt": last.dt,
                "open": fmt2(last.open),
                "high": fmt2(last.high),
                "low": fmt2(last.low),
                "close": fmt2(last.close),
            },
            "resistance": resistance,
            "support": support,
            "order_blocks": obs,
        }

    return result


# =========================
# Routes
# =========================
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


@app.get("/health")
def health():
    return {
        "ok": True,
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": APP_VERSION,
    }


@app.post("/structure")
def structure(req: StructureRequest):
    try:
        # ตรวจ TF ให้เป็นชุดที่รองรับ
        allowed = {"M5", "M15", "M30", "H1", "H4", "D1"}
        tfs = [t.upper() for t in req.tfs if t.upper() in allowed]
        if not tfs:
            raise HTTPException(status_code=400, detail="tfs must be one or more of M5,M15,M30,H1,H4,D1")

        return build_structure(req.symbol, tfs)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
