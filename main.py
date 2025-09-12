# main.py
import os
import re
from typing import List, Dict, Any
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-12.3"

# =========================
# Config & CORS
# =========================
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()

if _ALLOWED in ("", "*"):
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

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
    symbol: str = Field(..., description="เช่น XAUUSD หรือ XAU/USD")
    tfs: List[str] = Field(..., description="['M5','M15','M30','H1','H4','D1']")

    @field_validator("tfs")
    @classmethod
    def check_tfs(cls, v: List[str]) -> List[str]:
        allowed = {"M5", "M15", "M30", "H1", "H4", "D1"}
        vv = [x.upper() for x in v]
        bad = [x for x in vv if x not in allowed]
        if bad:
            raise ValueError(f"Unsupported TF: {bad}. Allowed: {sorted(list(allowed))}")
        return vv

# =========================
# Utilities
# =========================
def normalize_symbol(sym: str) -> str:
    s = sym.strip().upper()
    if "/" in s:
        return s
    letters = re.sub(r"[^A-Z]", "", s)
    if len(letters) == 6:
        return f"{letters[:3]}/{letters[3:]}"
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
    if tf not in mapping:
        raise ValueError(f"Unsupported TF: {tf}")
    return mapping[tf]

def fetch_series(symbol: str, tf: str, size: int = 250) -> List[Dict[str, Any]]:
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",   # ล่าสุดมาก่อน
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=25)
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TwelveData error: {e}")

    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(status_code=502, detail=str(data.get("message", "API error")))

    values = (data or {}).get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData")

    rows: List[Dict[str, Any]] = []
    for v in values:
        try:
            rows.append(
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

    if not rows:
        raise HTTPException(status_code=502, detail="Cannot parse bars")
    return rows  # ล่าสุด -> เก่า

# ---------- Swings ----------
def _is_swing_high(highs: List[float], i: int, k: int) -> bool:
    L = len(highs)
    left = max(0, i - k)
    right = min(L, i + k + 1)
    base = highs[i]
    for j in range(left, right):
        if j == i:
            continue
        if highs[j] >= base:
            return False
    return True

def _is_swing_low(lows: List[float], i: int, k: int) -> bool:
    L = len(lows)
    left = max(0, i - k)
    right = min(L, i + k + 1)
    base = lows[i]
    for j in range(left, right):
        if j == i:
            continue
        if lows[j] <= base:
            return False
    return True

def find_swings(bars_asc: List[Dict[str, Any]], k: int = 3) -> Dict[str, List[Dict[str, Any]]]:
    highs = [b["high"] for b in bars_asc]
    lows = [b["low"] for b in bars_asc]
    sh, sl = [], []
    for i in range(len(bars_asc)):
        if _is_swing_high(highs, i, k):
            sh.append({"idx": i, "price": highs[i], "dt": bars_asc[i]["dt"]})
        if _is_swing_low(lows, i, k):
            sl.append({"idx": i, "price": lows[i], "dt": bars_asc[i]["dt"]})
    return {"swing_highs": sh, "swing_lows": sl}

def compute_sr_from_swings(bars_desc: List[Dict[str, Any]], take: int = 3) -> Dict[str, Any]:
    bars_asc = list(reversed(bars_desc))  # เก่า -> ใหม่
    swings = find_swings(bars_asc, k=3)

    # เอาเฉพาะสวิงล่าสุด (เรียงใหม่ -> เก่า)
    sh_sorted = sorted(swings["swing_highs"], key=lambda x: x["idx"], reverse=True)[:take]
    sl_sorted = sorted(swings["swing_lows"], key=lambda x: x["idx"], reverse=True)[:take]

    resistances = [round(x["price"], 2) for x in sh_sorted]
    supports = [round(x["price"], 2) for x in sl_sorted]

    return {
        "resistances": resistances,
        "supports": supports,
        "resistance_value": (resistances[0] if resistances else round(max(b["high"] for b in bars_desc), 2)),
        "support_value": (supports[0] if supports else round(min(b["low"] for b in bars_desc), 2)),
    }

# ---------- Order Blocks ----------
def detect_order_blocks(bars_desc: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Heuristic:
      - หา swing ล่าสุด (จาก bars_asc)
      - หาก close ทะลุ swing high ล่าสุด -> bullish BOS
        -> หาแท่งแดงล่าสุด “ก่อนหน้า BOS” ภายใน 10 แท่ง แล้วสร้าง zone = [open, low]
      - หาก close ทะลุ swing low ล่าสุด -> bearish BOS
        -> หาแท่งเขียวล่าสุด “ก่อนหน้า BOS” ภายใน 10 แท่ง แล้วสร้าง zone = [high, open]
    ส่งกลับ { "bullish": {...} | None, "bearish": {...} | None }
    """
    bars_asc = list(reversed(bars_desc))
    swings = find_swings(bars_asc, k=3)
    if not swings["swing_highs"] and not swings["swing_lows"]:
        return {"bullish": None, "bearish": None}

    last_idx = len(bars_asc) - 1
    last_close = bars_asc[last_idx]["close"]

    # ---- Bullish BOS ----
    bull = None
    last_sh = max(swings["swing_highs"], key=lambda x: x["idx"]) if swings["swing_highs"] else None
    if last_sh and last_close > last_sh["price"]:
        # จุด BOS = จุดที่ปิดทะลุ (หาจากหลังมาหน้า)
        bos_i = None
        for i in range(last_idx, last_sh["idx"], -1):
            if bars_asc[i]["close"] > last_sh["price"]:
                bos_i = i
                break
        if bos_i is not None:
            # หาแท่งแดงล่าสุดก่อน bos_i ภายใน 10 แท่ง
            start = max(last_sh["idx"], bos_i - 10)
            ob_i = None
            for j in range(bos_i - 1, start - 1, -1):
                if bars_asc[j]["close"] < bars_asc[j]["open"]:
                    ob_i = j
                    break
            if ob_i is not None:
                ob_bar = bars_asc[ob_i]
                bull = {
                    "type": "bullish",
                    "bar": {
                        "dt": ob_bar["dt"],
                        "open": ob_bar["open"],
                        "high": ob_bar["high"],
                        "low": ob_bar["low"],
                        "close": ob_bar["close"],
                    },
                    "zone": [round(ob_bar["open"], 2), round(ob_bar["low"], 2)],  # [บน, ล่าง]
                }

    # ---- Bearish BOS ----
    bear = None
    last_sl = max(swings["swing_lows"], key=lambda x: x["idx"]) if swings["swing_lows"] else None
    if last_sl and last_close < last_sl["price"]:
        bos_i = None
        for i in range(last_idx, last_sl["idx"], -1):
            if bars_asc[i]["close"] < last_sl["price"]:
                bos_i = i
                break
        if bos_i is not None:
            start = max(last_sl["idx"], bos_i - 10)
            ob_i = None
            for j in range(bos_i - 1, start - 1, -1):
                if bars_asc[j]["close"] > bars_asc[j]["open"]:
                    ob_i = j
                    break
            if ob_i is not None:
                ob_bar = bars_asc[ob_i]
                bear = {
                    "type": "bearish",
                    "bar": {
                        "dt": ob_bar["dt"],
                        "open": ob_bar["open"],
                        "high": ob_bar["high"],
                        "low": ob_bar["low"],
                        "close": ob_bar["close"],
                    },
                    "zone": [round(ob_bar["high"], 2), round(ob_bar["open"], 2)],  # [บน, ล่าง]
                }

    return {"bullish": bull, "bearish": bear}

def build_tf_payload(norm_symbol: str, tf: str) -> Dict[str, Any]:
    bars_desc = fetch_series(norm_symbol, tf, size=250)
    last = bars_desc[0]

    sr = compute_sr_from_swings(bars_desc, take=3)
    ob = detect_order_blocks(bars_desc)

    return {
        "tf": tf,
        "last_bar": {
            "dt": last["dt"],
            "open": last["open"],
            "high": last["high"],
            "low": last["low"],
            "close": last["close"],
        },
        # R/S (แบบเดี่ยว + ลิสต์)
        "resistance": sr["resistance_value"],
        "support": sr["support_value"],
        "resistances": sr["resistances"],
        "supports": sr["supports"],
        # Order Blocks
        "order_blocks": ob,  # {"bullish": {...}|None, "bearish": {...}|None}
    }

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
    try:
        norm_symbol = normalize_symbol(req.symbol)
        results: List[Dict[str, Any]] = [build_tf_payload(norm_symbol, tf) for tf in req.tfs]
        return {"status": "OK", "symbol": norm_symbol, "results": results}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
