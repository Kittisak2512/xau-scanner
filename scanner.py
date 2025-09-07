# scanner.py
import os
import time
import math
import requests
import numpy as np

API_KEY = os.getenv("TWELVEDATA_API_KEY")

def _fetch(symbol: str, interval: str, outputsize: int = 200):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": API_KEY,
        "outputsize": outputsize,
        "order": "desc",
    }
    r = requests.get(url, params=params, timeout=15)
    j = r.json()
    if "values" not in j:
        return None
    vals = j["values"]
    # ล่าสุดอยู่ index 0 (เพราะ order=desc)
    # แปลงเป็น float
    for v in vals:
        for k in ("open", "high", "low", "close"):
            v[k] = float(v[k])
    return vals

def _prev_candle_box(values):
    """
    รับรายการแท่ง (order=desc) -> ใช้ 'แท่งก่อนหน้า' ที่ index=1
    คืนค่า (box_high, box_low) จาก open/close ของแท่งนั้น
    """
    if not values or len(values) < 2:
        return None
    prev = values[1]          # แท่งก่อนหน้า
    box_high = max(prev["open"], prev["close"])
    box_low  = min(prev["open"], prev["close"])
    return box_high, box_low

def _latest_price(values):
    return float(values[0]["close"])

def scan_breakout_v2(
    symbol="XAU/USD",
    higher_tf="1h",     # หรือ "4h"
    lower_tf="5min",    # หรือ "15min"
    buffer_points=0.20,     # กันชนสำหรับเงื่อนเบรก
    tolerance_points=0.50,  # ยอมให้แตะกรอบ +/- ค่านี้ ถือว่ารีเทส
    tp1_factor=0.5,
    tp2_factor=1.0,
    small_sl_buffer=0.0    # จะเพิ่มกันชน SL ก็ได้ เช่น 0.05
):
    # 1) ดึงข้อมูล
    higher_vals = _fetch(symbol, higher_tf, outputsize=50)
    lower_vals  = _fetch(symbol, lower_tf,  outputsize=200)

    if higher_vals is None or lower_vals is None:
        return {"status": "ERROR", "reason": "ไม่สามารถดึงข้อมูลได้"}

    # 2) ตีกรอบจากแท่งก่อนหน้าของ TF ใหญ่
    box = _prev_candle_box(higher_vals)
    if box is None:
        return {"status": "ERROR", "reason": "ข้อมูล TF ใหญ่ไม่พอ"}
    box_high, box_low = box
    box_height = box_high - box_low
    last = _latest_price(lower_vals)

    # 3) ตรวจเบรกเอาต์
    up_break   = last > (box_high + buffer_points)
    down_break = last < (box_low - buffer_points)

    # 4) ถ้าไม่เบรก → WATCH
    if not up_break and not down_break:
        return {
            "status": "WATCH",
            "reason": "ยังไม่เบรกกรอบ H1/H4",
            "ref": {
                "higher_tf": higher_tf,
                "lower_tf": lower_tf,
                "box_high": round(box_high, 2),
                "box_low": round(box_low, 2),
                "box_height": round(box_height, 2),
                "last": round(last, 2),
            }
        }

    # 5) ถ้าเบรกแล้ว → ตรวจรีเทส
    # นิยาม “รีเทส”: กลับมาใกล้เส้นกรอบภายใน tolerance หรือ retrace >= 50%
    # คำนวณจากราคาล่าสุด ๆ ใน TF เล็กช่วงหลังเบรก
    closes = [float(v["close"]) for v in lower_vals[:50]][::-1]  # กลับลำดับให้เก่า->ใหม่
    # หาแท่งที่เริ่มหลุดกรอบ
    break_idx = None
    if up_break:
        for i, c in enumerate(closes):
            if c > (box_high + buffer_points):
                break_idx = i
                break
        ref_line = box_high
        side = "BUY"
    else:
        for i, c in enumerate(closes):
            if c < (box_low - buffer_points):
                break_idx = i
                break
        ref_line = box_low
        side = "SELL"

    if break_idx is None:
        # ปกติจะต้องเจอ ถ้าไม่เจอถือว่าพึ่งเบรก
        return {
            "status": "BREAKOUT_WAIT_RETEST",
            "side": "BUY" if up_break else "SELL",
            "ref": {
                "higher_tf": higher_tf,
                "lower_tf": lower_tf,
                "box_high": round(box_high, 2),
                "box_low": round(box_low, 2),
                "last": round(last, 2),
            },
            "note": "เพิ่งเบรก แต่ยังหาจุดเบรกในประวัติล่าสุดไม่เจอ"
        }

    # high/low นับจากจุดเบรกจนถึงล่าสุด เพื่อวัด %retrace
    post = closes[break_idx:]  # ตั้งแต่แท่งเบรกเป็นต้นมา
    if side == "BUY":
        peak = max(post)
        retrace = (peak - last)
        full   = (peak - ref_line)
    else:
        trough = min(post)
        retrace = (last - trough)
        full   = (ref_line - trough)

    retrace_ratio = retrace / full if full > 0 else 0.0

    # เงื่อนไขรีเทส
    near_line = abs(last - ref_line) <= tolerance_points
    half_pull = retrace_ratio >= 0.5

    if not (near_line or half_pull):
        return {
            "status": "BREAKOUT_WAIT_RETEST",
            "side": side,
            "ref": {
                "higher_tf": higher_tf,
                "lower_tf": lower_tf,
                "box_high": round(box_high, 2),
                "box_low": round(box_low, 2),
                "box_height": round(box_height, 2),
                "last": round(last, 2),
            },
            "note": "เบรกแล้ว กำลังรอรีเทสเส้นกรอบหรือย่อ/เด้ง ≥ 50%"
        }

    # 6) พร้อมเข้าออเดอร์ → คำนวณ Entry/SL/TP
    entry = ref_line  # เข้าที่เส้นกรอบ
    if side == "BUY":
        sl  = box_low - small_sl_buffer
        tp1 = entry + tp1_factor * box_height
        tp2 = entry + tp2_factor * box_height
    else:
        sl  = box_high + small_sl_buffer
        tp1 = entry - tp1_factor * box_height
        tp2 = entry - tp2_factor * box_height

    return {
        "status": "ENTRY_READY",
        "side": side,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "ref": {
            "higher_tf": higher_tf,
            "lower_tf": lower_tf,
            "box_high": round(box_high, 2),
            "box_low": round(box_low, 2),
            "box_height": round(box_height, 2),
            "last": round(last, 2)
        },
        "note": "Breakout แล้วรีเทสเส้นกรอบ/50% — พร้อมเข้าออเดอร์"
    }

