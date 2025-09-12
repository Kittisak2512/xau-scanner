import os
from typing import List, Dict, Any
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_VERSION = "2025-09-12.struct-2"

# =========================
# Config
# =========================
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _ALLOWED == "*" or _ALLOWED == "":
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

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
    tfs: List[str] = Field(..., description="List of TFs: M5,M15,M30,H1,H4,D1")


# =========================
# Helpers
# =========================
TF_INTERVAL = {
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
    "D1": "1day",
}


def normalize_symbol(sym: str) -> str:
    s = sym.upper().replace(" ", "")
    # แปลงรูปแบบที่พบบ่อย
    mapping = {
        "XAUUSD": "XAU/USD",
        "XAGUSD": "XAG/USD",
        "EURUSD": "EUR/USD",
        "GBPUSD": "GBP/USD",
        "USDJPY": "USD/JPY",
        "USDCAD": "USD/CAD",
        "AUDUSD": "AUD/USD",
        "NZDUSD": "NZD/USD",
    }
    if s in mapping:
        return mapping[s]
    if "/" not in s and len(s) == 6:
        return s[:3] + "/" + s[3:]
    return s


def fetch_series(symbol: str, tf: str, size: int = 300) -> List[Dict[str, Any]]:
    if not TD_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")
    if tf not in TF_INTERVAL:
        raise HTTPException(status_code=400, detail=f"Unsupported TF: {tf}")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": TF_INTERVAL[tf],
        "outputsize": size,
        "order": "desc",
        "timezone": "UTC",
        "apikey": TD_API_KEY,
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

    # แปลงเป็น float/str ที่แน่นอน
    bars: List[Dict[str, Any]] = []
    for v in values:
        try:
            bars.append(
                {
                    "dt": v["datetime"],
                    "open": float(v["open"]),
                    "high": float(v["high"]),
                    "low": float(v["low"]),
                    "close": float(v["close"]),
                }
            )
        except Exception:
            continue
    if not bars:
        raise HTTPException(status_code=502, detail="Cannot parse bars")
    return bars  # เรียงจากใหม่ → เก่า


def last_closed(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    # TwelveData คืนแท่งที่ปิดแล้วเป็นตัวแรกตาม order=desc
    return bars[0]


def nearest_levels(bars: List[Dict[str, Any]], ref_price: float) -> Dict[str, Any]:
    """
    หา Support/Resistance จาก swing ภายในช่วง N แท่ง
    - Resistance = ค่าสูงสุดที่ 'สูงกว่า' ref_price และใกล้ที่สุด
    - Support    = ค่าต่ำสุดที่ 'ต่ำกว่า' ref_price และใกล้ที่สุด
    ถ้าไม่พบตามเงื่อนไข → คืน None (ให้ frontend แสดง "-" ได้)
    """
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]

    # ผู้สมัครใกล้ที่สุดที่อยู่คนละฝั่งราคา
    res_candidates = [h for h in highs if h > ref_price]
    sup_candidates = [l for l in lows if l < ref_price]

    resistance = min(res_candidates) if res_candidates else None
    support = max(sup_candidates) if sup_candidates else None

    return {
        "resistance": round(resistance, 2) if resistance is not None else None,
        "support": round(support, 2) if support is not None else None,
    }


def detect_order_blocks(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    OB แบบง่ายและปลอดภัย:
    - Bullish OB: หา 'down' candle (open > close) ที่ต่อมามี close สูงกว่าจุด high ของมันภายใน 3 แท่ง
      กล่อง = [low, high] ของ down candle นั้น
    - Bearish OB: หา 'up' candle (close > open) ที่ต่อมามี close ต่ำกว่า 'low' ของมันภายใน 3 แท่ง
      กล่อง = [low, high] ของ up candle นั้น
    คืนลิสต์ล่าสุดฝั่งละ 1 กล่อง (มากสุด 2)
    """
    ob_list: List[Dict[str, Any]] = []

    # หา Bullish
    for i in range(1, min(len(bars), 120)):
        c = bars[i]
        if c["open"] > c["close"]:  # down candle
            hi = c["high"]
            found = False
            for j in range(i - 1, max(i - 4, -1), -1):
                if bars[j]["close"] > hi:
                    found = True
                    break
            if found:
                ob_list.append({"type": "bullish", "low": round(c["low"], 2), "high": round(c["high"], 2)})
                break

    # หา Bearish
    for i in range(1, min(len(bars), 120)):
        c = bars[i]
        if c["close"] > c["open"]:  # up candle
            lo = c["low"]
            found = False
            for j in range(i - 1, max(i - 4, -1), -1):
                if bars[j]["close"] < lo:
                    found = True
                    break
            if found:
                ob_list.append({"type": "bearish", "low": round(c["low"], 2), "high": round(c["high"], 2)})
                break

    return ob_list


# =========================
# Routes
# =========================
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/structure")
def structure(req: StructureRequest):
    symbol = normalize_symbol(req.symbol)
    tfs = [tf.upper() for tf in req.tfs if tf.upper() in TF_INTERVAL]

    results = []
    errors = []

    for tf in tfs:
        try:
            bars = fetch_series(symbol, tf, size=300)
            last = last_closed(bars)
            ref_price = last["close"]

            lv = nearest_levels(bars[:180], ref_price)
            obs = detect_order_blocks(bars[:180])

            payload = {
                "tf": tf,
                "last_bar": last,
                "resistance": lv["resistance"],  # อาจเป็น None
                "support": lv["support"],        # อาจเป็น None
                "order_blocks": obs or [],       # เสมอเป็น list
            }
            results.append(payload)
        except HTTPException as e:
            errors.append({"tf": tf, "error": e.detail})
        except Exception as e:
            errors.append({"tf": tf, "error": str(e)})

    status = "OK" if results and not errors else ("PARTIAL" if results else "ERROR")
    return {
        "status": status,
        "symbol": symbol,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "errors": errors,
    }
