import os
import math
from typing import Optional, Literal, Dict, Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# -----------------------------
# Env & constants
# -----------------------------
TD_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
if not TD_API_KEY:
    # ให้รันได้แม้ลืมใส่ key (จะแจ้งเตือนในผลลัพธ์)
    TD_API_KEY = "MISSING_API_KEY"

ALLOWED = os.getenv("ALLOWED_ORIGINS", "*")
allow_origins = [o.strip() for o in ALLOWED.split(",") if o.strip()] or ["*"]

TD_BASE = "https://api.twelvedata.com"

# แมปชื่อ TF -> interval ของ TwelveData
INTERVAL_MAP = {
    "M5": "5min",
    "M15": "15min",
    "H1": "1h",
    "H4": "4h",
}

# ดึงไม่เกิน ~3 ชั่วโมงย้อนหลังสำหรับ TF ต่ำ
LOOKBACK_BARS = {
    "M5": 36,   # ~3 ชม.
    "M15": 12,  # ~3 ชม.
}

# -----------------------------
# FastAPI
# -----------------------------
app = FastAPI(title="xau-scanner", version="2025-09-09.3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -----------------------------
# Schemas
# -----------------------------
TFHigh = Literal["H1", "H4"]
TFLow = Literal["M5", "M15"]

class SignalRequest(BaseModel):
    symbol: str = Field(..., example="XAU/USD")
    tf_high: TFHigh = Field(..., example="H4")
    tf_low: TFLow = Field(..., example="M5")


# -----------------------------
# Helpers
# -----------------------------
def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


async def td_time_series(symbol: str, interval: str, outputsize: int = 50) -> Dict[str, Any]:
    """
    เรียก TwelveData /time_series
    คืน dict (raw JSON) | {"error": "..."} เมื่อผิดพลาด
    """
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": str(outputsize),
        "order": "DESC",
        "apikey": TD_API_KEY,
    }
    url = f"{TD_BASE}/time_series"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        data = r.json()
        if "status" in data and data["status"] == "error":
            return {"error": data.get("message", "twelvedata error")}
        return data


def previous_bar(values: list) -> Optional[dict]:
    """
    คืนแท่งที่ 'ปิดล่าสุด' จาก list ที่เรียง DESC ของ TwelveData
    ค่าใน values[0] คือแท่งล่าสุดที่ 'ปิดแล้ว' (TD ส่งค่าปิดมาเท่านั้น)
    """
    if not values or not isinstance(values, list):
        return None
    return values[0]


def last_n(values: list, n: int) -> list:
    if not values:
        return []
    return values[: max(0, n)]


def make_box_from_high_tf(prev_bar: dict) -> Optional[dict]:
    """สร้างกรอบจาก high/low ของแท่งก่อนหน้าของ TF สูง"""
    if not prev_bar:
        return None
    hi = _num(prev_bar.get("high"))
    lo = _num(prev_bar.get("low"))
    if hi is None or lo is None:
        return None
    # ให้ box_upper > box_lower เสมอ
    upper = max(hi, lo)
    lower = min(hi, lo)
    return {
        "datetime": prev_bar.get("datetime"),
        "high": hi,
        "low": lo,
        "box_upper": upper,
        "box_lower": lower,
    }


def detect_breakout_and_plan(values_low: list, box_upper: float, box_lower: float) -> dict:
    """
    ตรวจเบรกเอาท์จาก TF ต่ำ (list เรียง DESC: แท่งใหม่สุดอยู่ index 0)
    คืนผล:
    {
      "break": "UP"/"DOWN"/None,
      "at": close_price,
      "entry": suggested_entry,
      "entry_50": suggested_50_pullback,
      "sl": stop_loss,
      "tp1": target1,
      "tp2": target2,
      "last_closed": { ... }
    }
    """
    out = {
        "break": None,
        "last_closed": None,
        "at": None,
        "entry": None,
        "entry_50": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
    }
    if not values_low:
        return out

    last = values_low[0]
    out["last_closed"] = last
    close0 = _num(last.get("close"))
    if close0 is None:
        return out

    # เงื่อนไขเบรก
    if close0 > box_upper:
        out["break"] = "UP"
        out["at"] = close0
        # แผนเข้าเมื่อรีเทสต์ขอบบน
        out["entry"] = box_upper
        out["entry_50"] = box_upper + (close0 - box_upper) * 0.5
        # SL ใต้กรอบนิดนึง
        out["sl"] = box_upper - (box_upper - box_lower) * 0.25
        # TP อย่างง่าย: ระยะเท่าความกว้างกรอบ และ 1.5x
        box_height = (box_upper - box_lower)
        out["tp1"] = close0 + box_height * 1.0
        out["tp2"] = close0 + box_height * 1.5

    elif close0 < box_lower:
        out["break"] = "DOWN"
        out["at"] = close0
        out["entry"] = box_lower
        out["entry_50"] = box_lower - (box_lower - close0) * 0.5
        out["sl"] = box_lower + (box_upper - box_lower) * 0.25
        box_height = (box_upper - box_lower)
        out["tp1"] = close0 - box_height * 1.0
        out["tp2"] = close0 - box_height * 1.5

    return out


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": app.version, "ok": True}


@app.get("/health")
def health():
    return {"ok": True, "twelvedata_key": "set" if os.getenv("TWELVEDATA_API_KEY") else "missing"}


@app.post("/signal")
async def signal(req: SignalRequest):
    if TD_API_KEY == "MISSING_API_KEY":
        return {
            "status": "ERROR",
            "message": "TWELVEDATA_API_KEY is missing in environment.",
        }

    symbol = req.symbol
    tf_high = req.tf_high
    tf_low = req.tf_low

    # 1) ดึงแท่งล่าสุด (ปิดแล้ว) ของ TF สูง เพื่อทำกรอบจาก high/low
    data_high = await td_time_series(symbol, INTERVAL_MAP[tf_high], outputsize=2)
    if "error" in data_high:
        return {"status": "ERROR", "message": f"High TF fetch error: {data_high['error']}"}
    values_high = data_high.get("values") or []
    prev_h = previous_bar(values_high)  # ล่าสุดที่ปิดแล้ว
    box = make_box_from_high_tf(prev_h)
    if not box:
        return {"status": "ERROR", "message": "Cannot build high/low box from high TF."}

    # 2) ดึงแท่ง TF ต่ำ (3 ชม. ล่าสุด)
    lookback = LOOKBACK_BARS[tf_low]
    data_low = await td_time_series(symbol, INTERVAL_MAP[tf_low], outputsize=lookback)
    if "error" in data_low:
        return {"status": "ERROR", "message": f"Low TF fetch error: {data_low['error']}"}
    values_low = data_low.get("values") or []
    bars_low = last_n(values_low, lookback)

    # 3) ตรวจ Breakout + แผนเข้า (รีเทสต์ / 50%)
    plan = detect_breakout_and_plan(bars_low, box["box_upper"], box["box_lower"])

    # 4) สร้างผลลัพธ์และคำแนะนำ
    status = "OK"
    message = "WAIT — ราคายังไม่เบรกกรอบใน TF ต่ำ ภายใน 2–3 ชม."
    sig: Optional[str] = None
    entry = sl = tp1 = tp2 = None

    if plan["break"] == "UP":
        sig = "BUY"
        message = (
            "BREAKOUT ขึ้นเหนือกรอบบน — รอรีเทสต์เส้นกรอบบน (หรือ Pullback ~50%) เพื่อเข้าซื้อ"
        )
        entry = plan["entry"]
        sl = plan["sl"]
        tp1 = plan["tp1"]
        tp2 = plan["tp2"]

    elif plan["break"] == "DOWN":
        sig = "SELL"
        message = (
            "BREAKOUT ลงใต้กรอบล่าง — รอรีเทสต์เส้นกรอบล่าง (หรือ Pullback ~50%) เพื่อเข้าขาย"
        )
        entry = plan["entry"]
        sl = plan["sl"]
        tp1 = plan["tp1"]
        tp2 = plan["tp2"]

    response = {
        "status": status,
        "symbol": symbol,
        "tf_high": tf_high,
        "tf_low": tf_low,
        "message": message,
        "signal": sig,          # BUY / SELL / None
        "entry": entry,         # แผนเข้าที่เส้นกรอบ
        "entry_50": plan["entry_50"],  # แผนเข้าแบบ 50% ของระยะแตกต่าง
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "box": {
            "upper": box["box_upper"],
            "lower": box["box_lower"],
            "ref_bar": {
                "datetime": box["datetime"],
                "high": box["high"],
                "low": box["low"],
            },
            "hint": "กรอบจาก high/low ของแท่งก่อนหน้าใน TF สูง",
        },
        "overlay": {
            "note": "ใช้กรอบบน/ล่างประกอบกับ TF ต่ำ — รอรีเทสต์หรือ 50% pullback เพื่อเข้า",
            "trend_view": f"ดูทิศทางจาก H1/H4 (กรอบอ้างอิง {tf_high})",
        },
        "raw": {
            "last_closed_low_tf": plan["last_closed"],
        },
    }
    return response
