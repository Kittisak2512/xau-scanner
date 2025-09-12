# main.py
import os
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-12.2"

# ==========
# Config
# ==========
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()

if _ALLOWED in ("", "*"):
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# ==========
# App
# ==========
app = FastAPI(title="xau-scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========
# Models
# ==========
class StructureRequest(BaseModel):
    symbol: str = Field(..., examples=["XAUUSD", "XAU/USD"])
    tfs: List[str] = Field(..., examples=[["M5", "M15", "M30", "H1", "H4", "D1"]])

    @field_validator("tfs")
    @classmethod
    def check_tfs(cls, v: List[str]) -> List[str]:
        valid = {"M5", "M15", "M30", "H1", "H4", "D1"}
        out = []
        for x in v:
            x = x.upper()
            if x not in valid:
                raise ValueError(f"Unsupported TF: {x}")
            out.append(x)
        # ลดซ้ำ และคงลำดับเดิม
        seen, ordered = set(), []
        for x in out:
            if x not in seen:
                seen.add(x)
                ordered.append(x)
        return ordered


class Candle(BaseModel):
    dt: str
    open: float
    high: float
    low: float
    close: float


# ==========
# Utilities
# ==========
def normalize_symbol(sym: str) -> str:
    """
    แปลง XAUUSD -> XAU/USD (ถ้าเป็นรูปแบบ 6 ตัวอักษร)
    ไม่งั้นคืนค่าเดิม (เช่น EUR/USD ก็ปล่อยไว้)
    """
    s = sym.strip().upper().replace(" ", "")
    if "/" in s:
        return s
    if len(s) == 6 and s.isalpha():
        return f"{s[:3]}/{s[3:]}"
    return s


def td_interval(tf: str) -> str:
    mapping = {
        "M5": "5min",
        "M15": "15min",
        "M30": "30min",
        "H1": "1h",
        "H4": "4h",
        "D1": "1day",
    }
    return mapping[tf.upper()]


def fetch_series(symbol: str, tf: str, size: int = 400) -> List[Candle]:
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",      # ล่าสุดมาก่อน
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=25)
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(status_code=502, detail=str(data.get("message", "API error")))

    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData.")

    out: List[Candle] = []
    for v in values:
        try:
            out.append(
                Candle(
                    dt=v["datetime"],
                    open=float(v["open"]),
                    high=float(v["high"]),
                    low=float(v["low"]),
                    close=float(v["close"]),
                )
            )
        except Exception:
            # ข้ามแถวที่พาร์สไม่ได้
            continue
    if not out:
        raise HTTPException(status_code=502, detail="Cannot parse bars.")
    return out  # ล่าสุดมาก่อน (ปิดแล้ว)


def last_closed(bars: List[Candle]) -> Candle:
    # TwelveData ส่งมาเป็น bar ที่ "ปิดแล้ว" อยู่แล้วเมื่อ order=desc
    return bars[0]


def find_swing_levels(bars: List[Candle], lookback: int = 120) -> Dict[str, List[float]]:
    """
    หา swing-high / swing-low แบบเรียบง่าย:
      - swing-high: H[i] > H[i-1] และ H[i] > H[i+1]
      - swing-low:  L[i] < L[i-1] และ L[i] < L[i+1]
    bars เป็นลำดับล่าสุด -> เก่ากว่า (desc) ดังนั้น index 0 คือแท่งล่าสุด
    """
    highs, lows = [], []
    # ใช้ช่วงข้อมูลด้านหลัง (เก่ากว่า) เพื่อกันโน้ยส์จากแท่งล่าสุดเกินไป
    n = min(len(bars), lookback + 2)
    seq = list(reversed(bars[:n]))  # กลับให้เป็นเก่าสุด -> ล่าสุด เพื่ออ่านง่าย
    for i in range(1, len(seq) - 1):
        prev, cur, nxt = seq[i - 1], seq[i], seq[i + 1]
        if cur.high > prev.high and cur.high > nxt.high:
            highs.append(cur.high)
        if cur.low < prev.low and cur.low < nxt.low:
            lows.append(cur.low)
    return {"highs": highs, "lows": lows}


def nearest_resistance_above(close: float, candidates: List[float], min_gap: float) -> Optional[float]:
    # เลือกค่าที่ > close และต่างอย่างน้อย min_gap, เอาที่ใกล้ close ที่สุด
    ups = [x for x in candidates if x > close + min_gap]
    if not ups:
        return None
    return min(ups, key=lambda x: abs(x - close))


def nearest_support_below(close: float, candidates: List[float], min_gap: float) -> Optional[float]:
    downs = [x for x in candidates if x < close - min_gap]
    if not downs:
        return None
    return max(downs, key=lambda x: abs(x - close))


def tf_min_gap(tf: str) -> float:
    """
    ระยะกันโน้ยส์ขั้นต่ำระหว่าง Close กับ R/S (หน่วย "จุด" ตามฟีดของคุณ)
    ปรับได้ตามความเหมาะสม
    """
    tf = tf.upper()
    if tf == "M5":
        return 1.0
    if tf == "M15":
        return 1.5
    if tf == "M30":
        return 2.0
    if tf == "H1":
        return 2.0
    if tf == "H4":
        return 3.0
    if tf == "D1":
        return 5.0
    return 2.0


def detect_order_blocks(bars: List[Candle], lookback: int = 120) -> List[Dict[str, Any]]:
    """
    ตรวจจับ Order Blocks แบบเบสิคสุด ๆ เพื่อความเสถียร:
      - Bullish OB: แท่งแดง (open>close) แล้วถัดไป 2 แท่งเป็นเขียวยก high/close
      - Bearish OB: แท่งเขียว (close>open) แล้วถัดไป 2 แท่งเป็นแดงกด low/close
    คืนค่าเป็นโซน [low, high] ของแท่งต้นกำเนิด
    """
    zones: List[Dict[str, Any]] = []
    n = min(len(bars), lookback + 3)
    seq = list(reversed(bars[:n]))  # เก่าสุด -> ล่าสุด

    for i in range(len(seq) - 2):
        a, b, c = seq[i], seq[i + 1], seq[i + 2]
        # Bullish OB
        if a.open > a.close and b.close > b.open and c.close > c.open and b.high < c.high:
            low, high = min(a.open, a.close), max(a.open, a.close)
            zones.append({"type": "bullish", "zone": [round(low, 2), round(high, 2)]})
        # Bearish OB
        if a.close > a.open and b.close < b.open and c.close < c.open and b.low > c.low:
            low, high = min(a.open, a.close), max(a.open, a.close)
            zones.append({"type": "bearish", "zone": [round(low, 2), round(high, 2)]})

    # คัดล่าสุดไม่ให้เยอะเกิน
    return zones[-4:]


# ==========
# Core per TF
# ==========
def analyze_tf(symbol_norm: str, tf: str) -> Dict[str, Any]:
    bars = fetch_series(symbol_norm, tf, size=400)
    last = last_closed(bars)
    close = last.close

    swings = find_swing_levels(bars, lookback=160)
    min_gap = tf_min_gap(tf)

    # เลือกแนวที่ถูกด้าน (R > close, S < close) และห่างอย่างน้อย min_gap
    r = nearest_resistance_above(close, swings["highs"], min_gap)
    s = nearest_support_below(close, swings["lows"], min_gap)

    # ตรวจจับ OB
    ob = detect_order_blocks(bars, lookback=160)

    return {
        "tf": tf,
        "last_bar": {
            "dt": last.dt,
            "open": round(last.open, 2),
            "high": round(last.high, 2),
            "low": round(last.low, 2),
            "close": round(close, 2),
        },
        "resistance": round(r, 2) if r is not None else None,
        "support": round(s, 2) if s is not None else None,
        "order_blocks": ob,
    }


# ==========
# Routes
# ==========
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/structure")
def structure(req: StructureRequest):
    try:
        symbol_norm = normalize_symbol(req.symbol)
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        for tf in req.tfs:
            try:
                res = analyze_tf(symbol_norm, tf)
                results.append(res)
            except HTTPException as he:
                # เดินต่อ TF อื่น ๆ
                errors.append({"tf": tf, "error": f"{he.detail}"})
            except Exception as e:
                errors.append({"tf": tf, "error": str(e)})

        status = "OK" if results and not errors else ("PARTIAL" if results else "ERROR")
        return {
            "status": status,
            "symbol": symbol_norm,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "results": results,
            "errors": errors,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
