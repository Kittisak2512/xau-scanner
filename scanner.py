import numpy as np
import os
import requests

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

def scan_breakout(
    symbol="XAU/USD",
    higher_tf="1h",         # โซนเบรกเอาท์อ้างอิง H1/H4
    lower_tf="5min",        # ไทม์เฟรมเข้าออเดอร์ M5/M15
    buffer_H=120,           # จำนวนแท่งย้อนหลังเพื่อหา H_high/H_low
    retest_window_L=24,     # จำนวนแท่ง M5/M15 ล่าสุดที่ใช้ดูราคา current
    sl_points=250,          # SL (point)
    tp1_points=500,         # TP1 (point)
    tp2_points=1000         # TP2 (point)
):
    higher = fetch_data(symbol, interval=higher_tf, outputsize=200)
    lower  = fetch_data(symbol, interval=lower_tf,  outputsize=200)

    if higher is None or lower is None:
        return {"status": "ERROR", "reason": "ไม่สามารถดึงข้อมูลได้"}

    # หาโซนบน H1/H4
    H_high = float(np.max(higher[-buffer_H:]))
    H_low  = float(np.min(higher[-buffer_H:]))
    box_height = H_high - H_low

    # ราคาไทม์เฟรมเข้าออเดอร์ (M5/M15) ล่าสุด
    last = float(lower[-1])

    # เตรียมผลลัพธ์พื้นฐาน (อ้างอิง)
    base_ref = {
        "higher_tf": higher_tf,
        "lower_tf":  lower_tf,
        "H_high":    round(H_high, 2),
        "H_low":     round(H_low,  2),
        "box_height": round(box_height, 2),
        "last":      round(last,    2),
        "params": {
            "sl_points":  sl_points,
            "tp1_points": tp1_points,
            "tp2_points": tp2_points
        }
    }

    # กรณี Breakout ขึ้น
    if last > H_high:
        entry = H_high
        sl = max(H_low, entry - sl_points)            # SL ใต้ขอบบน หรืออีกทางคือ entry - sl_points
        tp1 = entry + tp1_points
        tp2 = entry + tp2_points

        rr1 = round((tp1 - entry) / max(1.0, (entry - sl)), 2)
        rr2 = round((tp2 - entry) / max(1.0, (entry - sl)), 2)

        return {
            "status": "OK",
            "signal": "BUY",
            "entry": round(entry, 2),
            "sl":    round(sl,    2),
            "tp1":   round(tp1,   2),
            "tp2":   round(tp2,   2),
            "rr":    {"tp1": rr1, "tp2": rr2},
            "ref":   base_ref,
            "note":  "Breakout ขึ้นจากโซน H1/H4 → เข้าออเดอร์ที่ขอบบน"
        }

    # กรณี Breakout ลง
    elif last < H_low:
        entry = H_low
        sl = min(H_high, entry + sl_points)           # SL เหนือขอบล่าง หรือ entry + sl_points
        tp1 = entry - tp1_points
        tp2 = entry - tp2_points

        rr1 = round((entry - tp1) / max(1.0, (sl - entry)), 2)
        rr2 = round((entry - tp2) / max(1.0, (sl - entry)), 2)

        return {
            "status": "OK",
            "signal": "SELL",
            "entry": round(entry, 2),
            "sl":    round(sl,    2),
            "tp1":   round(tp1,   2),
            "tp2":   round(tp2,   2),
            "rr":    {"tp1": rr1, "tp2": rr2},
            "ref":   base_ref,
            "note":  "Breakout ลงจากโซน H1/H4 → เข้าออเดอร์ที่ขอบล่าง"
        }

    # ยังไม่เบรก
    else:
        return {
            "status": "WATCH",
            "reason": "ยังไม่ Breakout โซน H1/H4",
            "ref": base_ref
        }

