from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import os, requests

app = FastAPI()

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGINS] if ALLOWED_ORIGINS != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/signal")
async def signal(data: dict):
    symbol = data.get("symbol", "XAU/USD")
    higher_tf = data.get("higher_tf", "H4")
    lower_tf = data.get("lower_tf", "M15")
    sl_points = data.get("sl_points", 250)
    tp1_points = data.get("tp1_points", 500)
    tp2_points = data.get("tp2_points", 1000)
    lookback = data.get("box_lookback", 20)

    # ดึงข้อมูล Higher TF
    url_high = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={higher_tf}&outputsize={lookback}&apikey={TWELVEDATA_API_KEY}"
    r_high = requests.get(url_high).json()
    if "values" not in r_high:
        return {"error": "fail fetch higher tf", "detail": r_high}

    highs = [float(x["high"]) for x in r_high["values"]]
    lows = [float(x["low"]) for x in r_high["values"]]
    H_high, H_low = max(highs), min(lows)

    # ดึงข้อมูล Lower TF
    url_low = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={lower_tf}&outputsize=2&apikey={TWELVEDATA_API_KEY}"
    r_low = requests.get(url_low).json()
    if "values" not in r_low:
        return {"error": "fail fetch lower tf", "detail": r_low}

    last_close = float(r_low["values"][0]["close"])
    last_time = r_low["values"][0]["datetime"]

    signal, reason = "WAIT", None
    entry, sl, tp1, tp2 = None, None, None, None

    if last_close > H_high:
        signal = "ENTRY LONG"
        entry = last_close
        sl, tp1, tp2 = entry - sl_points, entry + tp1_points, entry + tp2_points
    elif last_close < H_low:
        signal = "ENTRY SHORT"
        entry = last_close
        sl, tp1, tp2 = entry + sl_points, entry - tp1_points, entry - tp2_points
    else:
        reason = "Price inside box"

    return {
        "status": "OK",
        "signal": signal,
        "ref": {"higher_tf": higher_tf, "lower_tf": lower_tf,
                "H_high": H_high, "H_low": H_low, "last": last_close, "last_time": last_time},
        "params": {"sl_points": sl_points, "tp1_points": tp1_points, "tp2_points": tp2_points, "box_lookback": lookback},
        "prices": {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2} if entry else None,
        "reason": reason
    }
