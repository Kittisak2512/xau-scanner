from __future__ import annotations
from typing import Literal, Optional, Dict
from datetime import datetime, timezone
import os

import httpx
import pandas as pd
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware


APP_NAME = "xau-scanner"
APP_VERSION = "2025-09-07.1"

app = FastAPI(title=APP_NAME, version=APP_VERSION)

# ===== CORS =====
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # แนะนำใส่เฉพาะโดเมน Netlify ของคุณ
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Config =====
TF_TO_INTERVAL = {
    "M5": "5min",
    "M15": "15min",
    "H1": "1h",
    "H4": "4h",
}

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def ensure_interval(tf: str) -> str:
    if tf not in TF_TO_INTERVAL:
        raise HTTPException(400, f"Unsupported timeframe: {tf}")
    return TF_TO_INTERVAL[tf]

# ===== TwelveData adapter =====
async def fetch_ohlc_twelvedata(symbol: str, tf: str, api_key: str) -> pd.DataFrame:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": ensure_interval(tf),
        "outputsize": "200",
        "timezone": "UTC",
        "apikey": api_key,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        data = r.json()

    if "status" in data and data["status"] == "error":
        raise HTTPException(502, f"TwelveData error: {data.get('message')}")
    if "values" not in data:
        raise HTTPException(502, f"Unexpected TwelveData payload: {data}")

    df = pd.DataFrame(data["values"])
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df = df.sort_values("datetime").set_index("datetime")
    return df


# ===== Models =====
class ScanDataIn(BaseModel):
    symbol: str = "XAU/USD"
    higher_tf: Literal["H1", "H4"] = "H4"
    lower_tf: Literal["M5", "M15"] = "M15"
    sl_points: int = 250
    tp1_points: int = 500
    tp2_points: int = 1000
    api_key: Optional[str] = None

class EntryOut(BaseModel):
    side: Literal["long", "short"]
    price: float
    sl: float
    tp1: float
    tp2: float

class ScanDataOut(BaseModel):
    status: Literal["ENTRY", "WATCH"]
    reason: str
    higher_tf: str
    lower_tf: str
    ref: Dict[str, float]
    params: Dict[str, int]
    entry: Optional[EntryOut] = None
    order_line: Optional[str] = None
    ts: str = Field(default_factory=utcnow_iso)


# ===== Logic =====
def compute_break_close(
    df_higher: pd.DataFrame,
    df_lower: pd.DataFrame,
    higher_tf: str,
    lower_tf: str,
    sl_points: int,
    tp1_points: int,
    tp2_points: int,
) -> ScanDataOut:
    H_high = float(df_higher["high"].iloc[-1])
    H_low = float(df_higher["low"].iloc[-1])
    last_close = float(df_lower["close"].iloc[-1])

    if last_close > H_high:
        side = "long"
        entry = EntryOut(
            side=side,
            price=last_close,
            sl=last_close - sl_points,
            tp1=last_close + tp1_points,
            tp2=last_close + tp2_points,
        )
        return ScanDataOut(
            status="ENTRY",
            reason="Break + Close เหนือกรอบ",
            higher_tf=higher_tf,
            lower_tf=lower_tf,
            ref={"H_high": H_high, "H_low": H_low, "last": last_close},
            params={"sl_points": sl_points, "tp1_points": tp1_points, "tp2_points": tp2_points},
            entry=entry,
            order_line=f"ENTRY LONG @ {entry.price:.2f} | SL {entry.sl:.2f} | TP1 {entry.tp1:.2f} | TP2 {entry.tp2:.2f}",
        )
    elif last_close < H_low:
        side = "short"
        entry = EntryOut(
            side=side,
            price=last_close,
            sl=last_close + sl_points,
            tp1=last_close - tp1_points,
            tp2=last_close - tp2_points,
        )
        return ScanDataOut(
            status="ENTRY",
            reason="Break + Close ใต้กรอบ",
            higher_tf=higher_tf,
            lower_tf=lower_tf,
            ref={"H_high": H_high, "H_low": H_low, "last": last_close},
            params={"sl_points": sl_points, "tp1_points": tp1_points, "tp2_points": tp2_points},
            entry=entry,
            order_line=f"ENTRY SHORT @ {entry.price:.2f} | SL {entry.sl:.2f} | TP1 {entry.tp1:.2f} | TP2 {entry.tp2:.2f}",
        )
    else:
        return ScanDataOut(
            status="WATCH",
            reason="ยังไม่ Breakout",
            higher_tf=higher_tf,
            lower_tf=lower_tf,
            ref={"H_high": H_high, "H_low": H_low, "last": last_close},
            params={"sl_points": sl_points, "tp1_points": tp1_points, "tp2_points": tp2_points},
        )


# ===== API =====
@app.post("/scan-breakout-data", response_model=ScanDataOut)
async def scan_breakout_data(body: ScanDataIn):
    api_key = body.api_key or os.getenv("TWELVEDATA_API_KEY")
    if not api_key:
        raise HTTPException(400, "Missing TwelveData API key.")

    df_higher = await fetch_ohlc_twelvedata(body.symbol, body.higher_tf, api_key)
    df_lower = await fetch_ohlc_twelvedata(body.symbol, body.lower_tf, api_key)
    return compute_break_close(
        df_higher, df_lower,
        body.higher_tf, body.lower_tf,
        body.sl_points, body.tp1_points, body.tp2_points
    )


@app.get("/")
def root():
    return {"app": APP_NAME, "version": APP_VERSION, "ok": True}
