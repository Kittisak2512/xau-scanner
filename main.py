# main.py
import os
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-12.stable-A2"

# ------------------------------------------------------------
# Environment / Config
# ------------------------------------------------------------
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()

if _ALLOWED in ("", "*"):
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# ------------------------------------------------------------
# FastAPI App
# ------------------------------------------------------------
app = FastAPI(title="xau-scanner", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------
# Models
# ------------------------------------------------------------
class StructureRequest(BaseModel):
    symbol: str = Field(..., examples=["XAUUSD", "XAU/USD"])
    tfs: List[str] = Field(..., description="List of TFs e.g. ['M5','M15','M30','H1','H4','D1']")

    @field_validator("tfs")
    @classmethod
    def check_tfs(cls, v: List[str]) -> List[str]:
        ok = {"M5", "M15", "M30", "H1", "H4", "D1"}
        vv = [s.upper().strip() for s in v]
        bad = [s for s in vv if s not in ok]
        if bad:
            raise ValueError(f"Unsupported TF(s): {bad}. Allowed: {sorted(ok)}")
        return vv


class Candle(BaseModel):
    datetime: str
    open: float
    high: float
    low: float
    close: float


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def normalize_symbol(s: str) -> str:
    """
    แปลงสัญลักษณ์ให้ TwelveData เข้าใจได้แน่นอน
    - XAUUSD / XAU USD / XAU-USD / XAU_USD / GOLD -> XAU/USD
    - ถ้าเป็นรูปแบบ 6 ตัวอักษร (เช่น EURUSD) จะถูกแบ่งเป็น EUR/USD
    """
    s = (s or "").strip().upper()
    aliases = {
        "XAUUSD": "XAU/USD",
        "XAU-USD": "XAU/USD",
        "XAU_USD": "XAU/USD",
        "GOLD": "XAU/USD",
    }
    if s in aliases:
        return aliases[s]
    if " " in s and s.replace(" ", "") == "XAUUSD":
        return "XAU/USD"
    if "/" not in s and len(s) == 6:
        return s[:3] + "/" + s[3:]
    return s


def tf_to_interval(tf: str) -> str:
    mapping = {
        "M5": "5min",
        "M15": "15min",
        "M30": "30min",
        "H1": "1h",
        "H4": "4h",
        "D1": "1day",
    }
    if tf not in mapping:
        raise ValueError(f"Unsupported TF: {tf}")
    return mapping[tf]


def safe_float(x, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def fetch_series(symbol: str, tf: str, size: int = 200) -> List[Candle]:
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": tf_to_interval(tf),
        "outputsize": size,
        "order": "desc",           # ล่าสุดอยู่ก่อน
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=25)
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON")

    if isinstance(data, dict) and data.get("status") == "error":
        # ส่ง error ต้นทางออกไปแบบชัดเจน
        raise HTTPException(status_code=502, detail=data.get("message", "Upstream error."))

    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from upstream.")

    out: List[Candle] = []
    for v in values:
        c = Candle(
            datetime=v["datetime"],
            open=float(v["open"]),
            high=float(v["high"]),
            low=float(v["low"]),
            close=float(v["close"]),
        )
        out.append(c)
    return out  # ล่าสุด -> เก่า


# ------------------------------------------------------------
# Core analytics (โครงสร้าง: Resistance / Support / Order Blocks)
# ------------------------------------------------------------
def compute_nearest_levels(bars: List[Candle]) -> Dict[str, Optional[float]]:
    """
    หา Resistance / Support ใกล้เคียง 'ราคาปิดแท่งล่าสุด' โดยบังคับเงื่อนไข:
      - resistance > last_close
      - support    < last_close
    วิธีเลือก: ใช้ highs/lows ย้อนหลัง (lookback 120 แท่ง) แล้วหาค่าที่ 'ใกล้ที่สุด' ฝั่งบน/ล่าง
    """
    if not bars:
        return {"resistance": None, "support": None}

    last = bars[0]
    last_close = last.close
    highs = [b.high for b in bars[:120]]
    lows = [b.low for b in bars[:120]]

    above = [h for h in highs if h > last_close]
    below = [l for l in lows if l < last_close]

    resistance = min(above) if above else (max(highs) if highs else None)
    support = max(below) if below else (min(lows) if lows else None)

    return {
        "resistance": safe_float(resistance),
        "support": safe_float(support),
    }


def detect_order_blocks(bars: List[Candle], max_blocks: int = 4) -> List[Dict[str, Any]]:
    """
    ตรวจ OB แบบ conservative และกัน null:
      - Bullish OB: หา 'แท่งแดง' (close<open) ที่ก่อนหน้าการวิ่งขึ้นชัดเจน
        แล้วให้ช่วงโซน = [low, open] ของแท่งแดงนั้น
      - Bearish OB: ตรงข้าม → โซน = [open, high] ของแท่งเขียวนั้น
    หมายเหตุ: เป็น heuristic บาง ๆ เพื่อให้ได้โซนที่สม่ำเสมอและไม่คืน [null,null]
    """
    out: List[Dict[str, Any]] = []
    if len(bars) < 20:
        return out

    # ใช้ข้อมูลย้อนหลัง ~120 แท่ง (ล่าสุด -> เก่า)
    window = bars[:120][::-1]  # เก่าสุด -> ล่าสุด เพื่อหา pattern แบบเดินหน้า

    # หา impulse ขึ้น/ลงแบบง่าย ๆ
    ups = []
    downs = []
    for i in range(2, len(window)):
        # 3 แท่งเขียวติด = impulse ขึ้น
        if window[i-2].close > window[i-2].open and window[i-1].close > window[i-1].open and window[i].close > window[i].open:
            ups.append(i)
        # 3 แท่งแดงติด = impulse ลง
        if window[i-2].close < window[i-2].open and window[i-1].close < window[i-1].open and window[i].close < window[i].open:
            downs.append(i)

    # Bullish OB: หาแท่งแดงก่อนหน้า cluster เขียว
    for idx in ups[::-1]:  # เริ่มจากอันล่าสุด
        j = idx - 3
        if j <= 0:
            continue
        # ไล่กลับไปหา "แท่งแดงล่าสุด" ก่อน impulse
        k = j
        while k >= 0 and not (window[k].close < window[k].open):
            k -= 1
        if k >= 0:
            low = safe_float(window[k].low)
            open_ = safe_float(window[k].open)
            if low is not None and open_ is not None:
                lo, hi = sorted([low, open_])
                out.append({"type": "bullish", "zone": [round(lo, 2), round(hi, 2)]})
        if len(out) >= max_blocks:
            break

    # Bearish OB: หาแท่งเขียวก่อนหน้า cluster แดง
    for idx in downs[::-1]:
        j = idx - 3
        if j <= 0:
            continue
        k = j
        while k >= 0 and not (window[k].close > window[k].open):
            k -= 1
        if k >= 0:
            open_ = safe_float(window[k].open)
            high = safe_float(window[k].high)
            if open_ is not None and high is not None:
                lo, hi = sorted([open_, high])
                out.append({"type": "bearish", "zone": [round(lo, 2), round(hi, 2)]})
        if len(out) >= max_blocks:
            break

    # จำกัดจำนวนและจัดลำดับให้ล่าสุดอยู่หน้า (โซนใกล้ปัจจุบันมีลำดับก่อน)
    return out[:max_blocks]


def build_tf_block(symbol: str, tf: str) -> Dict[str, Any]:
    series = fetch_series(symbol, tf, size=200)
    last = series[0]
    lv = compute_nearest_levels(series)
    obs = detect_order_blocks(series)

    return {
        "tf": tf,
        "last_bar": {
            "dt": last.datetime,
            "open": round(last.open, 2),
            "high": round(last.high, 2),
            "low": round(last.low, 2),
            "close": round(last.close, 2),
        },
        "resistance": None if lv["resistance"] is None else round(lv["resistance"], 2),
        "support": None if lv["support"] is None else round(lv["support"], 2),
        "order_blocks": obs,  # list[{type, zone:[lo,hi]}]
    }


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


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
            block = build_tf_block(symbol, tf)
            results.append(block)
        except HTTPException as he:
            # ส่ง error ต้นทางต่อ TF นั้น ๆ แต่ไม่ล้มทั้งงาน
            errors.append({"tf": tf, "error": str(he.detail)})
        except Exception as e:
            errors.append({"tf": tf, "error": str(e)})

    status = "OK"
    if errors and results:
        status = "PARTIAL"
    elif errors and not results:
        # ไม่มีผลลัพธ์เลย
        status = "ERROR"

    return {
        "status": status,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "results": results,
        "errors": errors,
    }
