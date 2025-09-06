import requests
import numpy as np
import os

API_KEY = os.getenv("TWELVEDATA_API_KEY")

def fetch_data(symbol="XAU/USD", interval="5min", outputsize=200):
    url = f"https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": API_KEY,
        "outputsize": outputsize
    }
    r = requests.get(url, params=params)
    data = r.json()
    if "values" not in data:
        return None
    values = data["values"]
    values.reverse()
    closes = np.array([float(v["close"]) for v in values], dtype=float)
    return closes

def scan_breakout(symbol="XAU/USD", higher_tf="1h", lower_tf="5min",
                  buffer_H=120, retest_window_L=24, sl_points=12, tp1_points=25, tp2_points=50):
    higher = fetch_data(symbol, interval=higher_tf, outputsize=200)
    lower = fetch_data(symbol, interval=lower_tf, outputsize=200)

    if higher is None or lower is None:
        return {"status": "ERROR", "reason": "ไม่สามารถดึงข้อมูลได้"}

    H_high, H_low = np.max(higher[-buffer_H:]), np.min(higher[-buffer_H:])

    last_L = lower[-1]

    if last_L > H_high:  # Breakout ขึ้น
        entry = H_high
        sl = entry - sl_points
        tp1 = entry + tp1_points
        tp2 = entry + tp2_points
        return {"status": "OK", "signal": "BUY", "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2}

    elif last_L < H_low:  # Breakout ลง
        entry = H_low
        sl = entry + sl_points
        tp1 = entry - tp1_points
        tp2 = entry - tp2_points
        return {"status": "OK", "signal": "SELL", "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2}

    else:
        return {"status": "WATCH", "reason": "ยังไม่เบรก H1/H4"}

