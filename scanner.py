# scanner.py
import os
import requests
import numpy as np

API_KEY = os.getenv("TWELVEDATA_API_KEY")

def fetch_data(symbol="XAU/USD", interval="15min", outputsize=200):
    """
    ดึงแท่งราคาจาก Twelve Data: returns dict {closes, highs, lows} เป็น np.array (เรียงจากเก่า -> ใหม่)
    """
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": API_KEY
    }
    r = requests.get(url, params=params, timeout=15)
    data = r.json()
    if "values" not in data:
        return None
    vals = data["values"]
    vals.reverse()  # เรียงจากเก่า -> ใหม่
    closes = np.array([float(v["close"]) for v in vals], dtype=float)
    highs  = np.array([float(v["high"])  for v in vals], dtype=float)
    lows   = np.array([float(v["low"])   for v in vals], dtype=float)
    return {"closes": closes, "highs": highs, "lows": lows}

def scan_breakout(
    symbol="XAU/USD",
    higher_tf="1h",        # ใช้ "1h" หรือ "4h"
    lower_tf="5min",       # ใช้ "5min" หรือ "15min"
    lookback_h=100,        # ช่วงหาจุด high/low ย้อนหลังใน TF สูง
    sl_points=20,
    tp1_points=25,
    tp2_points=50
):
    """
    กลยุทธ์ Breakout:
    1) หาค่า H_high/H_low จาก TF สูง (H1/H4) ในกรอบ lookback_h
    2) เช็ค close ล่าสุดของ TF ต่ำ (M5/M15)
    3) ถ้าทะลุ high → BUY, ถ้าทะลุ low → SELL
       กำหนด entry = เส้น breakout, SL/TP เป็น points (หน่วยเดียวกับราคา)
    """
    # 1) TF สูง
    higher = fetch_data(symbol=symbol, interval=higher_tf, outputsize=max(200, lookback_h + 5))
    if higher is None:
        return {"status": "ERROR", "reason": f"ไม่สามารถดึงข้อมูล TF สูง {higher_tf} ได้"}

    highs_h = higher["highs"][-lookback_h:]
    lows_h  = higher["lows"] [-lookback_h:]
    H_high  = float(np.max(highs_h))
    H_low   = float(np.min(lows_h))

    # 2) TF ล่าง
    lower = fetch_data(symbol=symbol, interval=lower_tf, outputsize=200)
    if lower is None:
        return {"status": "ERROR", "reason": f"ไม่สามารถดึงข้อมูล TF ต่ำ {lower_tf} ได้"}

    last_close = float(lower["closes"][-1])

    # 3) ตัดสินใจ + คำนวณจุด
    if last_close > H_high:   # Breakout ขึ้น
        entry = H_high
        sl  = entry - sl_points
        tp1 = entry + tp1_points
        tp2 = entry + tp2_points
        return {
            "status": "OK",
            "signal": "BUY",
            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
            "ref": {"higher_tf": higher_tf, "lower_tf": lower_tf, "H_high": H_high, "H_low": H_low}
        }

    if last_close < H_low:    # Breakout ลง
        entry = H_low
        sl  = entry + sl_points
        tp1 = entry - tp1_points
        tp2 = entry - tp2_points
        return {
            "status": "OK",
            "signal": "SELL",
            "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
            "ref": {"higher_tf": higher_tf, "lower_tf": lower_tf, "H_high": H_high, "H_low": H_low}
        }

    # ยังไม่ทะลุ
    return {
        "status": "WATCH",
        "reason": "ยังไม่ Breakout โซน H1/H4",
        "ref": {"higher_tf": higher_tf, "lower_tf": lower_tf, "H_high": H_high, "H_low": H_low, "last": last_close}
    }
