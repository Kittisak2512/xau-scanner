import os
import requests
import numpy as np

API_KEY = os.getenv("TWELVEDATA_API_KEY")  # ใส่คีย์ไว้ใน Render > Environment

def fetch_data(symbol="XAU/USD", interval="5min", outputsize=200):
    """ดึงราคาจาก TwelveData (close series)"""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": API_KEY,
        "outputsize": outputsize,
    }
    r = requests.get(url, params=params, timeout=20)
    data = r.json()
    if "values" not in data:
        return None
    values = data["values"]
    values.reverse()  # ทำให้ index -1 เป็นแท่งล่าสุด
    closes = np.array([float(v["close"]) for v in values], dtype=float)
    return closes

def scan_breakout(
    symbol="XAU/USD",
    higher_tf="1h",
    lower_tf="5min",
    buffer_H=120,
    retest_window_L=24,
    sl_points=250,
    tp1_points=500,
    tp2_points=1000,
):
    """
    หาจุด Breakout: ใช้ High/Low จากกรอบ H1/H4 (ผ่าน higher_tf) แล้วดูราคา real-time จาก lower_tf (M5/M15)
    - ถ้าทะลุ High -> BUY
    - ถ้าทะลุ Low  -> SELL
    - ถ้ายังไม่ทะลุ -> WATCH
    พร้อมคำนวณ SL/TP เป็น “ราคา” ให้ด้วย
    """
    higher = fetch_data(symbol, interval=higher_tf, outputsize=buffer_H + 5)
    lower  = fetch_data(symbol, interval=lower_tf, outputsize=retest_window_L + 5)

    if higher is None or lower is None:
        return {
            "status": "ERROR",
            "reason": "ไม่สามารถดึงข้อมูลได้ (ตรวจ API KEY/โควต้า/สัญลักษณ์)",
        }

    H_high = float(np.max(higher[-buffer_H:]))  # กล่องบน H1/H4
    H_low  = float(np.min(higher[-buffer_H:]))

    last_L = float(lower[-1])  # ราคาล่าสุดฝั่ง M5/M15
    box_h  = H_high - H_low

    # ผลลัพธ์ ref/params พื้นฐาน
    ref = {
        "higher_tf": higher_tf,
        "lower_tf": lower_tf,
        "H_high": round(H_high, 2),
        "H_low": round(H_low, 2),
        "box_height": round(box_h, 2),
        "last": round(last_L, 2),
    }
    params = {
        "sl_points": sl_points,
        "tp1_points": tp1_points,
        "tp2_points": tp2_points,
    }

    # กฎ Breakout
    if last_L > H_high:
        entry = H_high
        sl    = entry - sl_points
        tp1   = entry + tp1_points
        tp2   = entry + tp2_points
        return {
            "status": "OK",
            "signal": "BUY",
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "ref": ref,
            "params": params,
        }

    if last_L < H_low:
        entry = H_low
        sl    = entry + sl_points
        tp1   = entry - tp1_points
        tp2   = entry - tp2_points
        return {
            "status": "OK",
            "signal": "SELL",
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "ref": ref,
            "params": params,
        }

    return {
        "status": "WATCH",
        "reason": "ยังไม่ Breakout โซน H1/H4",
        "ref": ref,
        "params": params,
    }
