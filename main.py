# main.py
import os
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

APP_VERSION = "2025-09-12.2"

# =========================
# Config / Environment
# =========================
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _ALLOWED in ("", "*"):
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# =========================
# App
# =========================
app = FastAPI(title="xau-scanner")

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
    symbol: str = Field(..., examples=["XAU/USD", "XAUUSD"])
    tfs: Optional[List[str]] = Field(
        None,
        description="Timeframes in {M5,M15,M30,H1,H4,D1}. If omitted -> all.",
        examples=[["M5", "M15", "M30", "H1", "H4", "D1"]],
    )

    @field_validator("tfs")
    @classmethod
    def check_tfs(cls, v):
        if v is None:
            return None
        allowed = {"M5", "M15", "M30", "H1", "H4", "D1"}
        vv = [s.upper() for s in v]
        for s in vv:
            if s not in allowed:
                raise ValueError(f"Unsupported tf: {s}")
        return vv


# =========================
# Utilities
# =========================

def normalize_symbol(raw: str) -> str:
    """
    Auto-convert user input symbols to TwelveData format.
    - 'XAUUSD'  -> 'XAU/USD'
    - 'eurusd'  -> 'EUR/USD'
    - 'XAU/USD' stays the same
    If has suffix like ':CUR' or ':FOREX', keep it.
    """
    s = (raw or "").strip().upper().replace(" ", "")
    if not s:
        return s

    # If already has '/', just return
    if "/" in s:
        return s

    # If contains suffix like ':CUR', ':FOREX', split first
    suffix = ""
    if ":" in s:
        base, suf = s.split(":", 1)
        s = base
        suffix = ":" + suf

    # If exactly 6 alphabetic chars -> insert slash 3/3
    if len(s) == 6 and s.isalpha():
        s = f"{s[:3]}/{s[3:]}"

    return s + suffix


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
        raise ValueError(f"Unsupported TF: {tf}")
    return mapping[m]


def fetch_series(symbol: str, tf: str, size: int = 200) -> List[Dict[str, Any]]:
    """
    Fetch candles (latest first) from TwelveData.
    """
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    r = requests.get(url, params=params, timeout=20)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON")

    if isinstance(data, dict) and data.get("status") == "error":
        # bubble upstream error message to frontend
        raise HTTPException(status_code=502, detail=str(data.get("message", "API error")))
    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData")

    # parse to floats and keep only necessary fields
    out = []
    for v in values:
        try:
            out.append(
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
    if not out:
        raise HTTPException(status_code=502, detail="Cannot parse bars")
    return out  # latest first


def compute_support_resistance(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Lightweight support/resistance:
    - resistance: max of last N highs
    - support: min of last N lows
    N uses all bars fetched (already small ~200, latest first).
    """
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    resistance = round(max(highs), 2)
    support = round(min(lows), 2)
    return {"resistance": resistance, "support": support}


# =========================
# Core
# =========================
ALL_TFS = ["M5", "M15", "M30", "H1", "H4", "D1"]

def build_tf_payload(norm_symbol: str, tf: str) -> Dict[str, Any]:
    bars = fetch_series(norm_symbol, tf, size=200)  # latest first
    sr = compute_support_resistance(bars)

    last = bars[0]
    payload = {
        "tf": tf,
        "last_bar": {
            "dt": last["dt"],
            "open": last["open"],
            "high": last["high"],
            "low": last["low"],
            "close": last["close"],
        },
        "resistance": sr["resistance"],
        "support": sr["support"],
        "order_blocks": [],  # kept for compatibility; you can fill later if needed
    }
    return payload


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
        # normalize symbol for TwelveData
        norm_symbol = normalize_symbol(req.symbol)

        tfs = req.tfs or ALL_TFS
        results: List[Dict[str, Any]] = []
        for tf in tfs:
            results.append(build_tf_payload(norm_symbol, tf))

        return {
            "status": "OK",
            "symbol_input": req.symbol,
            "symbol": norm_symbol,
            "tfs": tfs,
            "results": results,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
