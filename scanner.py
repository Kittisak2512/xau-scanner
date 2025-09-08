import os, math
from datetime import datetime, timezone
import pandas as pd
import httpx
from fastapi import HTTPException

TD_BASE = "https://api.twelvedata.com"
TD_KEY = os.getenv("TWELVEDATA_API_KEY")  # ตั้งใน Render dashboard

# map ชื่อ TF UI -> TwelveData interval
INTERVAL_MAP = {
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "1h",
    "H4": "4h",
}

async def fetch_ohlc(symbol: str, interval: str, outputsize: int = 500) -> pd.DataFrame:
    if not TD_KEY:
        raise HTTPException(status_code=500, detail="TWELVEDATA_API_KEY not set")
    params = {
        "symbol": symbol,
        "interval": interval,
        "apikey": TD_KEY,
        "outputsize": outputsize,
        "format": "JSON",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{TD_BASE}/time_series", params=params)
        data = r.json()
        if "values" not in data:
            raise HTTPException(status_code=502, detail=f"TwelveData error: {data}")
        df = pd.DataFrame(data["values"])
        # columns: datetime, open, high, low, close, volume
        df["datetime"] = pd.to_datetime(df["datetime"])
        for col in ["open","high","low","close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("datetime").reset_index(drop=True)
        return df

def breakout_logic(h_df: pd.DataFrame, l_df: pd.DataFrame):
    """
    กติกา: กล่องจาก TF สูง (H1/H4) = high/low ของแท่งล่าสุด
    ถ้าแท่งล่าสุดของ TF ต่ำ (M5/M15) 'ปิด' ทะลุนอกกรอบ ให้ ENTRY ทันที
    """
    if len(h_df) < 2 or len(l_df) < 2:
        return {"status":"WAIT","reason":"ข้อมูลน้อยเกินไป"}

    # กล่องจากแท่งล่าสุด TF สูง
    H_high = float(h_df.iloc[-1]["high"])
    H_low  = float(h_df.iloc[-1]["low"])
    last_close = float(l_df.iloc[-1]["close"])

    if last_close > H_high:
        side = "LONG"
        reason = "Break + Close เหนือกล่อง TF สูง"
    elif last_close < H_low:
        side = "SHORT"
        reason = "Break + Close ใตั้กล่อง TF สูง"
    else:
        return {
            "status":"WATCH",
            "reason": "ยังไม่ Breakout โซน H1/H4",
            "ref":{"higher_tf_high":H_high,"higher_tf_low":H_low,"last":last_close}
        }
    return {"status":"ENTRY", "side":side, "reason":reason,
            "ref":{"higher_tf_high":H_high,"higher_tf_low":H_low,"last":last_close}
           }

def price_from_points(entry: float, points: int, side: str, tp: bool) -> float:
    # points = หน่วย “พอยท์” (เช่น ทองคำ XAU/USD = 1 point = 1.0)
    if side == "LONG":
        return entry + points if tp else entry - points
    else:
        return entry - points if tp else entry + points

async def compute_signal(symbol: str, higher_tf: str, lower_tf: str,
                         sl_points: int, tp1_points: int, tp2_points: int):
    h_int = INTERVAL_MAP[higher_tf]
    l_int = INTERVAL_MAP[lower_tf]

    h_df = await fetch_ohlc(symbol, h_int, 300)
    l_df = await fetch_ohlc(symbol, l_int, 300)

    br = breakout_logic(h_df, l_df)
    if br.get("status") != "ENTRY":
        # ส่งผลสรุประหว่างรอ
        return br | {"higher_tf": higher_tf, "lower_tf": lower_tf}

    side = br["side"]
    last = float(l_df.iloc[-1]["close"])

    sl  = price_from_points(last, sl_points, side, tp=False)
    tp1 = price_from_points(last, tp1_points, side, tp=True)
    tp2 = price_from_points(last, tp2_points, side, tp=True)

    txt = f"ENTRY {side} @ {last:.2f} | SL {sl:.2f} | TP1 {tp1:.2f} | TP2 {tp2:.2f}"
    return {
        "status": "ENTRY",
        "side": side,
        "entry": last,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "message": txt,
        "higher_tf": higher_tf,
        "lower_tf": lower_tf,
        "box": br["ref"],
    }
