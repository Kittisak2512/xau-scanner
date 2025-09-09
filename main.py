# --- PATCH START: robust high-TF + breakout logic (drop-in) ---

import os, math, datetime as dt
import httpx
from fastapi import HTTPException

TWELVE_BASE = "https://api.twelvedata.com/time_series"
TZ = "Etc/UTC"
API_KEY = os.getenv("TWELVEDATA_API_KEY", "")

def _td_params(symbol:str, interval:str, output:int=120):
    return {
        "symbol": symbol,
        "interval": interval,            # "1h","4h","15min","5min"
        "outputsize": output,            # ขอเผื่อ 50-200 แท่ง
        "timezone": TZ,
        "order": "desc",
        "apikey": API_KEY,
    }

async def fetch_series(symbol:str, interval:str, output:int=120):
    """ดึงซีรีส์จาก TwelveData แบบกันเหนียว, คืน values:list ของแท่งปิดล่าสุดเรียงใหม่สุดก่อน"""
    params = _td_params(symbol, interval, output)
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(TWELVE_BASE, params=params)
    data = r.json()
    # กรณี error/ratelimit
    if "status" in data and data.get("status") == "error":
        raise HTTPException(status_code=502, detail=f"TwelveData error: {data.get('message','')}")
    vals = data.get("values", [])
    # บางเวลาจะไม่มีข้อมูล -> คืนลิสต์ว่าง
    return vals

def latest_closed(values):
    """รับ values (desc) -> คืนแท่งปิดล่าสุด (dict) หรือ None"""
    return values[0] if values else None

def hl_from_bar(bar):
    return float(bar["high"]), float(bar["low"])

def num(x): 
    try: return float(x)
    except: return None

def midpoint(a,b): 
    return (a+b)/2.0

async def build_box(symbol:str):
    """
    พยายามสร้างกรอบจาก H1 (หลัก) และ H4 (เพื่อกำหนด bias).
    ถ้า H4 ไม่มี -> ไม่เป็นไร ใช้ H1;
    ถ้า H1 ไม่มี -> สังเคราะห์จาก M15 4 ชั่วโมงล่าสุด (16 แท่ง),
    และถ้ายังไม่มี -> จาก M5 1 ชั่วโมงล่าสุด (12 แท่ง)
    """
    # 1) ดึง H1
    h1_vals = await fetch_series(symbol, "1h", output=10)
    h1_bar = latest_closed(h1_vals)
    box_high = box_low = None

    if h1_bar:
        hh, ll = hl_from_bar(h1_bar)
        box_high, box_low = hh, ll

    # 2) ดึง H4 (เพื่อ bias ทิศทาง ถ้ามี)
    h4_vals = await fetch_series(symbol, "4h", output=10)
    h4_bar = latest_closed(h4_vals)
    trend_bias = None
    if h4_bar:
        h4o = num(h4_bar["open"]); h4c = num(h4_bar["close"])
        if h4o is not None and h4c is not None:
            trend_bias = "UP" if h4c > h4o else "DOWN"

    # 3) ถ้า H1 ไม่มีเลย -> สังเคราะห์จาก lower TF
    if box_high is None or box_low is None:
        # ลองจาก M15 ~ 4 ชั่วโมงล่าสุด
        m15 = await fetch_series(symbol, "15min", output=20)
        if len(m15) >= 16:
            highs = [num(v["high"]) for v in m15[:16] if num(v["high"]) is not None]
            lows  = [num(v["low"])  for v in m15[:16] if num(v["low"])  is not None]
            if highs and lows:
                box_high, box_low = max(highs), min(lows)
        # เผื่อสุดท้ายจาก M5 ~ 1 ชั่วโมง
        if box_high is None or box_low is None:
            m5 = await fetch_series(symbol, "5min", output=15)
            if len(m5) >= 12:
                highs = [num(v["high"]) for v in m5[:12] if num(v["high"]) is not None]
                lows  = [num(v["low"])  for v in m5[:12] if num(v["low"])  is not None]
                if highs and lows:
                    box_high, box_low = max(highs), min(lows)

    if box_high is None or box_low is None:
        return None  # ให้ผู้เรียกจัดการข้อความ

    return {
        "box_high": box_high,
        "box_low":  box_low,
        "trend_bias": trend_bias,   # "UP"/"DOWN"/None
        "h1_bar": h1_bar,
        "h4_bar": h4_bar,
    }

async def scan_breakout(symbol:str, lower_tf:str):
    """
    โจทย์: เฝ้า M5/M15 → ถ้าเบรกกรอบ H1 (หรือสังเคราะห์) ให้สัญญาณทันที
    และแนะนำแผนเข้า: รีเทสขอบ หรือ 50% ของแท่งเบรก
    """
    # เตรียมกรอบ
    box = await build_box(symbol)
    if not box:
        return {"status":"ERROR", "message":"ไม่พอสำหรับสร้างกรอบจาก H1/H4 และ lower TF."}

    box_high = box["box_high"]; box_low = box["box_low"]

    # ดึง lower TF ล่าสุด ~ 2–3 ชม. สำหรับวิเคราะห์
    interval = "15min" if lower_tf.upper() == "M15" else "5min"
    look = 36 if interval=="5min" else 12  # ~3ชม.ที่ M5 หรือ ~3ชม.ที่ M15
    lows = await fetch_series(symbol, interval, output=look+2)
    if not lows:
        return {"status":"ERROR", "message":"ไม่มีข้อมูล lower timeframe."}

    last = latest_closed(lows)
    last_close = num(last["close"]); last_high = num(last["high"]); last_lowv = num(last["low"])
    broke_up = (last_close is not None and last_close > box_high)
    broke_dn = (last_close is not None and last_close < box_low)

    signal = None
    entry = sl = tp1 = tp2 = None
    note = None

    if broke_up:
        signal = "BUY"
        # จุดเข้า: รอรีเทสกรอบบน หรือ 50% ของแท่งเบรก
        entry_ret = box_high
        entry_50  = midpoint(last_lowv, last_high) if (last_lowv is not None and last_high is not None) else box_high
        entry = {"retest": round(entry_ret,2), "fifty": round(entry_50,2)}
        sl = round(box_low, 2)
    elif broke_dn:
        signal = "SELL"
        entry_ret = box_low
        entry_50  = midpoint(last_lowv, last_high) if (last_lowv is not None and last_high is not None) else box_low
        entry = {"retest": round(entry_ret,2), "fifty": round(entry_50,2)}
        sl = round(box_high, 2)

    # ถ้ายังไม่เบรก → รายงานแนวรับ/ต้านรอ
    if not signal:
        return {
            "status": "WAIT",
            "symbol": symbol,
            "tf": lower_tf.upper(),
            "support": round(box_low,2),
            "resistance": round(box_high,2),
            "message": "รอราคาเบรกกรอบบน/ล่าง แล้วค่อยรีเทสหรือย้อน ~50% เพื่อเข้าออเดอร์",
        }

    # คำนวณ TP แบบ Risk:Reward 1:1, 1:2 (ให้เหมาะกับ 2–3 ชม.)
    risk = abs(entry["retest"] - sl)
    if risk <= 0:
        tp1 = tp2 = None
    else:
        if signal == "BUY":
            tp1 = round(entry["retest"] + risk*1.0, 2)
            tp2 = round(entry["retest"] + risk*2.0, 2)
        else:
            tp1 = round(entry["retest"] - risk*1.0, 2)
            tp2 = round(entry["retest"] - risk*2.0, 2)

    return {
        "status": "OK",
        "symbol": symbol,
        "tf": lower_tf.upper(),
        "signal": signal,
        "box": {"upper": round(box_high,2), "lower": round(box_low,2)},
        "entry": entry,           # มีทั้ง retest และ 50%
        "sl": round(sl,2) if sl else None,
        "tp1": tp1, "tp2": tp2,
        "note": "เข้าเมื่อรีเทสเส้นกรอบ หรือ 50% ของแท่งเบรก ภายใน 2–3 ชม.",
        "bias": box["trend_bias"],
    }

# --- PATCH END ---
