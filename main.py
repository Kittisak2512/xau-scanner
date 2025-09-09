from fastapi import FastAPI
from pydantic import BaseModel
import requests
import os
from datetime import datetime

app = FastAPI()

API_KEY = os.getenv("TWELVEDATA_API_KEY", "")
BASE_URL = "https://api.twelvedata.com/time_series"

class SignalRequest(BaseModel):
    symbol: str
    tf: str   # "M5" or "M15"

@app.post("/signal")
def get_signal(req: SignalRequest):
    tf = req.tf
    params = {
        "symbol": req.symbol,
        "interval": tf,
        "apikey": API_KEY,
        "outputsize": 10
    }
    r = requests.get(BASE_URL, params=params)
    data = r.json()

    if "values" not in data:
        return {"status": "ERROR", "detail": data}

    bars = data["values"]
    latest = bars[0]
    prev = bars[1]

    high = float(latest["high"])
    low = float(latest["low"])
    close = float(latest["close"])

    # Logic ง่าย ๆ: Break + Close เกิน high/low
    signal = "WAIT"
    entry, sl, tp = None, None, None
    if close > float(prev["high"]):
        signal = "BUY"
        entry = close
        sl = low
        tp = round(entry + (entry - sl), 2)
    elif close < float(prev["low"]):
        signal = "SELL"
        entry = close
        sl = high
        tp = round(entry - (sl - entry), 2)

    return {
        "status": "OK",
        "symbol": req.symbol,
        "tf": tf,
        "signal": signal,
        "entry_price": entry,
        "sl_price": sl,
        "tp_price": tp,
        "latest_bar": latest,
        "time": datetime.utcnow().isoformat()
    }

@app.get("/")
def root():
    return {"app": "xau-scanner", "version": "2025-09-09.3", "ok": True}
