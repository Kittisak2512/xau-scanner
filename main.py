# main.py
import os
from typing import List, Dict, Any, Literal

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_VERSION = "2025-09-10.2-flex"

# =========================
# Environment & CORS
# =========================
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _ALLOWED in ("", "*"):
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# =========================
# FastAPI App
# =========================
app = FastAPI(title="xau-scanner", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Models
# =========================
class SignalRequest(BaseModel):
    """รูปแบบเดิม: ใช้ TF ต่ำอันเดียว (M5/M15)"""
    symbol: str = Field(..., examples=["XAU/USD", "XAUUSD"])
    tf: Literal["M5", "M15"]


class BreakoutRequest(BaseModel):
    """รูปแบบใหม่: ส่ง TF สูง + TF ต่ำ"""
    symbol: str = Field(..., examples=["XAU/USD", "XAUUSD"])
    tf_high: Literal["H1", "H4"]
    tf_low: Literal["M5", "M15"]


class Candle(BaseModel):
    dt: str
    open: float
    high: float
    low: float
    close: float


# =========================
# Utilities
# =========================
def td_interval(tf: str) -> str:
    m = tf.upper()
    mapping = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
    }
    if m not in mapping:
        raise ValueError(f"Unsupported TF: {tf}")
    return mapping[m]


def fetch_series(symbol: str, tf: str, size: int = 120) -> List[Candle]:
    """ดึงแท่งจาก TwelveData (ล่าสุดอยู่หน้าสุด / order=desc)"""
    if not TWELVEDATA_API_KEY:
        raise HTTPException(status_code=500, detail="Missing TWELVEDATA_API_KEY")

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": td_interval(tf),
        "outputsize": size,
        "order": "desc",
        "timezone": "UTC",
        "apikey": TWELVEDATA_API_KEY,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON.")

    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(status_code=502, detail=str(data.get("message", "API error")))
    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData.")

    out: List[Candle] = []
    for v in values:
        try:
            out.append(
                Candle(
                    dt=v["datetime"],
                    open=float(v["open"]),
                    high=float(v["high"]),
                    low=float(v["low"]),
                    close=float(v["close"]),
                )
            )
        except Exception:
            # ข้ามแถวที่พาร์สไม่ได้
            continue

    if not out:
        raise HTTPException(status_code=502, detail="Cannot parse bars.")
    return out  # ล่าสุดอยู่ index 0


def previous_closed(candles: List[Candle]) -> Candle:
    """ใช้แท่งปิดล่าสุด (TwelveData คืนค่ามาเป็นแท่งปิดเรียงย้อน)"""
    return candles[0]


def crossed_above(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close <= level < last_close


def crossed_below(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close >= level > last_close


def near(value: float, target: float, tolerance_points: float) -> bool:
    return abs(value - target) <= tolerance_points


# =========================
# Core Logic (แบบเดิม /signal)
# =========================
def analyze_signal(symbol: str, low_tf: str) -> Dict[str, Any]:
    """
    เวอร์ชันเดิม:
      1) เอา H1/H4 → resistance=max(high), support=min(low)
      2) ดู TF ต่ำ (M5/M15) เช็คเบรกด้วย close cross
      3) ถ้าเบรก -> สร้างสัญญาณ + entry/sl/tp
      4) ถ้าย่อตามเงื่อนไข -> ENTRY_READY
    """
    # H1/H4 box
    h1 = previous_closed(fetch_series(symbol, "H1", 50))
    h4 = previous_closed(fetch_series(symbol, "H4", 50))
    resistance = max(h1.high, h4.high)
    support = min(h1.low, h4.low)

    # Low TF
    bars = fetch_series(symbol, low_tf, 30)
    if len(bars) < 2:
        return {"status": "ERROR", "message": "Not enough low timeframe data."}
    last, prev = bars[0], bars[1]

    sl_points, tp1_points, tp2_points = 250.0, 500.0, 1000.0

    res: Dict[str, Any] = {
        "status": "OK",
        "symbol": symbol,
        "tf": low_tf,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "overlay": {
            "h1": h1.model_dump(),
            "h4": h4.model_dump(),
            "last": last.model_dump(),
            "prev": prev.model_dump(),
        },
        "signal": None,
        "entry": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "message": "",
    }

    up_break = crossed_above(prev.close, last.close, resistance)
    dn_break = crossed_below(prev.close, last.close, support)

    if up_break:
        body = abs(last.close - prev.close)
        entry = resistance
        tol = max(100.0, 0.5 * body)
        if near(last.close, entry, tol):
            res["signal"] = "ENTRY_READY_LONG"
            res["message"] = "Price retesting resistance after breakout."
        else:
            res["signal"] = "BREAKOUT_LONG"
            res["message"] = "Breakout above resistance. Wait pullback to enter."
        res["entry"] = round(entry, 2)
        res["sl"] = round(entry - sl_points, 2)
        res["tp1"] = round(entry + tp1_points, 2)
        res["tp2"] = round(entry + tp2_points, 2)
        return res

    if dn_break:
        body = abs(last.close - prev.close)
        entry = support
        tol = max(100.0, 0.5 * body)
        if near(last.close, entry, tol):
            res["signal"] = "ENTRY_READY_SHORT"
            res["message"] = "Price retesting support after breakout."
        else:
            res["signal"] = "BREAKOUT_SHORT"
            res["message"] = "Breakdown below support. Wait pullback to enter."
        res["entry"] = round(entry, 2)
        res["sl"] = round(entry + sl_points, 2)
        res["tp1"] = round(entry - tp1_points, 2)
        res["tp2"] = round(entry - tp2_points, 2)
        return res

    res["message"] = "WAIT — รอราคาเบรกกรอบบน/ล่าง (ภายใน 2–3 ชม. ที่ TF ต่ำ)."
    return res


# =========================
# Core Logic (แบบใหม่ /breakout - FLEX)
# =========================
def analyze_breakout(symbol: str, tf_high: str, tf_low: str) -> Dict[str, Any]:
    """
    Breakout ยืดหยุ่น:
      - wick breakout (high/low ทะลุกรอบ + buffer)
      - close breakout (close ทะลุกรอบ + buffer)
      - close cross สองแท่ง (แบบเดิม) + epsilon
      - รีเทสต์แล้วให้ ENTRY_READY_*
    """
    # --- สร้างกรอบจาก tf_high ---
    ref = previous_closed(fetch_series(symbol, tf_high, 50))
    upper = ref.high
    lower = ref.low

    # --- แท่ง tf_low ล่าสุด ---
    bars = fetch_series(symbol, tf_low, 30)
    if len(bars) < 2:
        return {"status": "ERROR", "message": "Not enough low timeframe data."}
    last, prev = bars[0], bars[1]

    # พารามิเตอร์
    sl_points, tp1_points, tp2_points = 250.0, 500.0, 1000.0
    buffer_points = 10.0   # ปรับได้ 5–20 ตามโบรก/สเปรด/ความไว
    eps = 1e-6             # กันเท่ากันเป๊ะ ๆ

    # โครงผลลัพธ์
    res: Dict[str, Any] = {
        "status": "OK",
        "symbol": symbol,
        "tf_high": tf_high,
        "tf_low": tf_low,
        "box": {
            "upper": round(upper, 2),
            "lower": round(lower, 2),
            "ref_bar": ref.model_dump(),
        },
        "overlay": {
            "last": last.model_dump(),
            "prev": prev.model_dump(),
        },
        "signal": None,
        "entry": None,
        "entry_50": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "message": "",
    }

    # ---------- เงื่อนไข breakout ทั้ง 3 ชั้น ----------
    wick_up  = last.high >= upper + buffer_points - eps
    wick_dn  = last.low  <= lower - buffer_points + eps

    close_up = last.close >= upper + buffer_points - eps
    close_dn = last.close <= lower - buffer_points + eps

    cross_up = (prev.close <= upper + eps) and (last.close >= upper + eps)
    cross_dn = (prev.close >= lower - eps) and (last.close <= lower - eps)

    # รวมเงื่อนไข
    up_break = wick_up or close_up or cross_up
    dn_break = wick_dn or close_dn or cross_dn

    # ฟังก์ชันช่วยคำนวณผลลัพธ์
    def pack_long(entry_level: float) -> Dict[str, Any]:
        body = abs(last.close - prev.close)
        tol = max(100.0, 0.5 * body)
        result = res.copy()
        result["entry"] = round(entry_level, 2)
        result["entry_50"] = round(entry_level - 0.5 * body, 2)
        result["sl"] = round(entry_level - sl_points, 2)
        result["tp1"] = round(entry_level + tp1_points, 2)
        result["tp2"] = round(entry_level + tp2_points, 2)
        if near(last.close, entry_level, tol):
            result["signal"] = "ENTRY_READY_LONG"
            result["message"] = "Retest เหนือกรอบบนหลังเบรก – เข้าได้"
        else:
            result["signal"] = "BREAKOUT_LONG"
            result["message"] = "เบรกกรอบบนแล้ว – รอรีเทสต์เพื่อเข้า"
        return result

    def pack_short(entry_level: float) -> Dict[str, Any]:
        body = abs(last.close - prev.close)
        tol = max(100.0, 0.5 * body)
        result = res.copy()
        result["entry"] = round(entry_level, 2)
        result["entry_50"] = round(entry_level + 0.5 * body, 2)
        result["sl"] = round(entry_level + sl_points, 2)
        result["tp1"] = round(entry_level - tp1_points, 2)
        result["tp2"] = round(entry_level - tp2_points, 2)
        if near(last.close, entry_level, tol):
            result["signal"] = "ENTRY_READY_SHORT"
            result["message"] = "Retest ใต้กรอบล่างหลังเบรก – เข้าได้"
        else:
            result["signal"] = "BREAKOUT_SHORT"
            result["message"] = "เบรกกรอบล่างแล้ว – รอรีเทสต์เพื่อเข้า"
        return result

    # ---------- ตัดสินใจ ----------
    if up_break:
        return pack_long(upper)

    if dn_break:
        return pack_short(lower)

    # ยังไม่เบรก
    res["message"] = "WAIT — รอราคาเบรกกรอบบน/ล่าง (ที่ TF ต่ำ)."
    return res


# =========================
# Routes
# =========================
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


@app.get("/health")
def health():
    return {"ok": True}


# ✅ เอนด์พอยน์ต์ใหม่ (ใช้ tf_high + tf_low)
@app.post("/breakout")
def breakout(req: BreakoutRequest):
    try:
        return analyze_breakout(req.symbol, req.tf_high, req.tf_low)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ✅ ทำ /signal ให้รองรับทั้งรูปแบบเก่า/ใหม่ (backward compatible)
@app.post("/signal")
def signal_compat(payload: Dict[str, Any]):
    """
    รองรับ:
      {symbol, tf}                 -> วิเคราะห์แบบเดิม (H1/H4 ผสม)
      {symbol, tf_high, tf_low}    -> วิเคราะห์แบบ breakout ใหม่ (เลือก H1/H4 ตรง ๆ)
    """
    try:
        if "tf" in payload:
            req = SignalRequest(**payload)
            return analyze_signal(req.symbol, req.tf)
        elif "tf_high" in payload and "tf_low" in payload:
            req = BreakoutRequest(**payload)
            return analyze_breakout(req.symbol, req.tf_high, req.tf_low)
        else:
            raise HTTPException(
                status_code=422,
                detail="Require either (symbol, tf) or (symbol, tf_high, tf_low).",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
