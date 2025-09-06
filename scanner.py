# scanner.py
# Breakout strategy: context on H1/H4, entry on M5/M15
# Data source: TwelveData (https://twelvedata.com/docs)

import os
import time
import math
import requests
import numpy as np
from typing import Dict, List, Optional, Tuple


# ======== Config ========
API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
BASE_URL = "https://api.twelvedata.com/time_series"

# ค่าเริ่มต้น สามารถปรับได้จากพารามิเตอร์ของ scan_breakout
DEFAULT_SYMBOL   = "XAU/USD"
DEFAULT_HIGHER_TF = "1h"     # หรือ "4h"
DEFAULT_ENTRY_TF  = "5min"   # หรือ "15min"

# กำหนดจำนวนแท่งที่ใช้หาโซน (บน TF สูง) และกันสัญญาณหลอก
DEFAULT_ZONE_LOOKBACK = 120  # แท่งบน TF สูงที่ใช้หา zone
DEFAULT_RETEST_GAP    = 24   # เว้นแท่งล่าสุดบน TF สูง (กันการใช้ high/low ที่เพิ่งเกิด)

# จุด SL/TP ในหน่วยราคา (ตาม instrument) – ผู้ใช้ปรับตามตลาดที่เทรด
DEFAULT_SL_POINTS  = 12
DEFAULT_TP1_POINTS = 25
DEFAULT_TP2_POINTS = 50

# เงื่อนไข breakout (กันหลอก): ให้ปิดเหนือแนวต้าน/ใต้แนวรับ ด้วยระยะกันเผื่อ
DEFAULT_BREAK_BUFFER = 0.0      # ถ้าต้องการเผื่อ 0.2, 0.5 ฯลฯ ใส่ได้
DEFAULT_CONFIRM_WITH_CLOSE = True  # ใช้ราคา close ของแท่งล่าสุดในการยืนยัน


# ======== Helpers ========

def _fetch_series(symbol: str, interval: str, outputsize: int = 200) -> Optional[Dict[str, List[float]]]:
    """
    เรียก TwelveData แล้วคืน dict: {time, open, high, low, close}
    เรียงตามเวลา (เก่าสุด -> ใหม่สุด)
    """
    if not API_KEY:
        return None

    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": API_KEY,
        "outputsize": outputsize,
        "format": "JSON",
    }
    try:
        r = requests.get(BASE_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "values" not in data:
            return None

        vals = list(reversed(data["values"]))  # Chronological
        t  = [v["datetime"] for v in vals]
        op = [float(v["open"])  for v in vals]
        hi = [float(v["high"])  for v in vals]
        lo = [float(v["low"])   for v in vals]
        cl = [float(v["close"]) for v in vals]
        return {"time": t, "open": op, "high": hi, "low": lo, "close": cl}
    except Exception:
        return None


def _recent_zone_from_higher_tf(
    hi: List[float],
    lo: List[float],
    lookback: int,
    retest_gap: int
) -> Tuple[float, float]:
    """
    หาโซนแนวรับ/แนวต้านจาก TF สูง:
      - ใช้ window ย้อนหลัง lookback แท่ง
      - ไม่รวมแท่งล่าสุด retest_gap แท่ง (กัน bias)
    คืนค่า (resistance, support)
    """
    n = len(hi)
    if n < (lookback + retest_gap + 5):
        # ข้อมูลน้อยไป ใช้ทั้งหมดเท่าที่มี
        start = 0
        end = max(1, n - retest_gap)
    else:
        start = n - (lookback + retest_gap)
        end   = n - retest_gap

    window_hi = hi[start:end]
    window_lo = lo[start:end]
    resistance = float(np.max(window_hi))
    support    = float(np.min(window_lo))
    return resistance, support


def _entry_decision(
    entry_close: float,
    resistance: float,
    support: float,
    break_buffer: float = DEFAULT_BREAK_BUFFER
) -> str:
    """
    ตัดสินใจสัญญาณจากราคาปิดของ TF เข้า:
      - ปิดเหนือ resistance + buffer → BUY
      - ปิดใต้ support - buffer → SELL
      - ไม่งั้น WAIT
    """
    if entry_close > (resistance + break_buffer):
        return "BUY"
    if entry_close < (support - break_buffer):
        return "SELL"
    return "WAIT"


def _build_targets(
    side: str,
    entry: float,
    sl_points: float,
    tp1_points: float,
    tp2_points: float
) -> Tuple[float, float, float]:
    """
    คำนวณ SL / TP1 / TP2 แบบระยะคงที่ (points เป็นหน่วยราคา)
    """
    if side == "BUY":
        sl  = entry - sl_points
        tp1 = entry + tp1_points
        tp2 = entry + tp2_points
    else:  # SELL
        sl  = entry + sl_points
        tp1 = entry - tp1_points
        tp2 = entry - tp2_points
    return sl, tp1, tp2


# ======== Public function ========

def scan_breakout(
    symbol: str = DEFAULT_SYMBOL,
    higher_tf: str = DEFAULT_HIGHER_TF,   # "1h" หรือ "4h"
    entry_tf: str  = DEFAULT_ENTRY_TF,    # "5min" หรือ "15min"
    zone_lookback: int = DEFAULT_ZONE_LOOKBACK,
    retest_gap: int    = DEFAULT_RETEST_GAP,
    sl_points: float   = DEFAULT_SL_POINTS,
    tp1_points: float  = DEFAULT_TP1_POINTS,
    tp2_points: float  = DEFAULT_TP2_POINTS,
    break_buffer: float = DEFAULT_BREAK_BUFFER,
    confirm_with_close: bool = DEFAULT_CONFIRM_WITH_CLOSE
) -> Dict:
    """
    กลยุทธ์ Breakout:
      1) ดึงข้อมูล TF สูง → หาโซนแนวรับ/แนวต้าน
      2) ดึงข้อมูล TF เข้า → ใช้แท่งล่าสุดตัดสินใจ breakout
      3) สร้าง Entry/SL/TP และคืน JSON

    หมายเหตุ: ถ้า TWELVEDATA_API_KEY ไม่ตั้งค่า → คืน ERROR
    """
    if not API_KEY:
        return {"status": "ERROR", "reason": "Missing TWELVEDATA_API_KEY env."}

    # 1) Higher TF
    higher = _fetch_series(symbol, higher_tf, outputsize=max(zone_lookback + retest_gap + 10, 220))
    if not higher:
        return {"status": "ERROR", "reason": f"Cannot fetch higher TF ({higher_tf})"}

    resistance, support = _recent_zone_from_higher_tf(
        higher["high"], higher["low"], zone_lookback, retest_gap
    )

    # 2) Entry TF (ใช้แท่งล่าสุด)
    entry_data = _fetch_series(symbol, entry_tf, outputsize=200)
    if not entry_data:
        return {"status": "ERROR", "reason": f"Cannot fetch entry TF ({entry_tf})"}

    last_close = float(entry_data["close"][-1])
    last_high  = float(entry_data["high"][-1])
    last_low   = float(entry_data["low"][-1])

    # ใช้อะไรยืนยัน breakout?
    price_for_decision = last_close if confirm_with_close else float(entry_data["open"][-1])

    side = _entry_decision(price_for_decision, resistance, support, break_buffer)

    if side == "WAIT":
        return {
            "status": "WATCH",
            "reason": {
                "note": "ยังไม่ทะลุ zone H1/H4",
                "higher_tf": higher_tf,
                "entry_tf": entry_tf,
                "resistance": resistance,
                "support": support,
                "last_close": last_close
            }
        }

    # 3) Entry/SL/TP
    entry_price = last_close  # เลือก entry เป็นปิดแท่งล่าสุดของ TF เข้า
    sl, tp1, tp2 = _build_targets(side, entry_price, sl_points, tp1_points, tp2_points)

    return {
        "status": "OK",
        "signal": side,
        "entry": round(entry_price, 5),
        "sl":    round(sl, 5),
        "tp1":   round(tp1, 5),
        "tp2":   round(tp2, 5),
        "info": {
            "symbol": symbol,
            "higher_tf": higher_tf,
            "entry_tf": entry_tf,
            "resistance": resistance,
            "support": support,
            "confirm_with_close": confirm_with_close,
            "break_buffer": break_buffer
        }
    }
