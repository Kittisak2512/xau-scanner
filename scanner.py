import requests
import pandas as pd

def scan_signal(symbol, higher_tf, lower_tf, sl, tp1, tp2, api_key):
    try:
        url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={lower_tf}&apikey={api_key}&outputsize=50"
        resp = requests.get(url)
        data = resp.json()

        if "values" not in data:
            return {"error": "API error", "detail": data}

        df = pd.DataFrame(data["values"])
        df["close"] = df["close"].astype(float)

        last = df.iloc[0]
        price = last["close"]

        signal = {
            "symbol": symbol,
            "entry": price,
            "sl": price - sl,
            "tp1": price + tp1,
            "tp2": price + tp2
        }

        return {"signal": signal}

    except Exception as e:
        return {"error": str(e)}
