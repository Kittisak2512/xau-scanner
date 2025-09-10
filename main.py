import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator, model_validator

APP_VERSION = "2025-09-10.2"

# =====================
# Environment / Config
# =====================
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _ALLOWED in ("", "*"):
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# =====================
# FastAPI app
# =====================
app = FastAPI(title="xau-scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =====================
# Models
# =====================
class Candle(BaseModel):
    dt: str
    open: float
    high: float
    low: float
    close: float


class SignalRequest(BaseModel):
    # รุ่นใหม่จากหน้าเว็บ: ส่ง symbol, tf_high, tf_low
    # รุ่นเก่าบางหน้าส่ง: symbol, tf  (ถือเป็น tf_low)
    symbol: str
    tf_high: Optional[str] = None
    tf_low: Optional[str] = None
    tf: Optional[str] = None  # backward-compat

    @model_validator(mode="after")
    def normalize(self) -> "SignalRequest":
        # แปลง tf -> tf_low (รองรับ client เก่า)
        if not self.tf_low and self.tf:
            self.tf_low = self.tf

        # ค่าเริ่มต้นอัตโนมัติ: ถ้าไม่ส่ง tf_high มา
        # - M5 -> ใช้ H1 เป็นกรอบ
        # - M15 -> ใช้ H4 เป็นกรอบ
        if self.tf_low:
            tl = self.tf_low.upper()
            if tl not in {"M5", "M15"}:
                raise ValueError("tf_low must be M5 or M15")
            if not self.tf_high:
                self.tf_high = "H1" if tl == "M5" else "H4"

        if not self.tf_low:
            raise ValueError("tf_low (or tf) is required.")
        if not self.tf_high:
            raise ValueError("tf_high is required.")

        self.tf_low = self.tf_low.upper()
        self.tf_high = self.tf_high.upper()
        if self.tf_high not in {"H1", "H4"}:
            raise ValueError("tf_high must be H1 or H4")
        return self


# =====================
# Utilities
# =====================
def td_interval(tf: str) -> str:
    m = tf.upper()
    mapping = {"M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h"}
    if m not in mapping:
        raise ValueError(f"Unsupported TF: {tf}")
    return mapping[m]


def fetch_series(symbol: str, tf: str, size: int) -> List[Candle]:
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
    r = requests.get(url, params=params, timeout=20)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON")

    if isinstance(data, dict) and data.get("status") == "error":
        raise HTTPException(status_code=502, detail=str(data.get("message", "API error")))
    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData")

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
            continue
    if not out:
        raise HTTPException(status_code=502, detail="Cannot parse bars")
    return out  # latest first (desc)


def last_closed(candles: List[Candle]) -> Candle:
    return candles[0]


def crossed_above(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close <= level < last_close


def crossed_below(prev_close: float, last_close: float, level: float) -> bool:
    return prev_close >= level > last_close


def near(value: float, target: float, tolerance_points: float) -> bool:
    return abs(value - target) <= tolerance_points


# =====================
# Core
# =====================
def analyze_breakout(symbol: str, tf_high: str, tf_low: str) -> Dict[str, Any]:
    """
    กติกา:
      1) ใช้แท่งที่ 'ปิดล่าสุด' ของ TF สูง (H1/H4) เป็นกรอบ: upper=high, lower=low
      2) เฝ้า TF ต่ำ (M5/M15) ตรวจ breakout (อนุโลมภายใน 2–3 แท่งล่าสุด)
      3) ถ้า breakout:
            - entry  : เส้นกรอบที่ถูกเบรก
            - entry_50: ใกล้กรอบ 50% ของช่วงกรอบ (เผื่อรีเทสต์ลึก)
            - SL     : 250 จุด
            - TP1/TP2: 500 / 1000 จุด
    """
    # --- High TF box from last closed bar ---
    hi_bar = last_closed(fetch_series(symbol, tf_high, 6))
    upper = hi_bar.high
    lower = hi_bar.low

    # --- Low TF recent bars ---
    low_bars = fetch_series(symbol, tf_low, 12)  # ~ 2–3 แท่งล่าสุดพอ
    if len(low_bars) < 3:
        return {"status": "ERROR", "message": "Not enough low timeframe data."}

    last_b = low_bars[0]
    prev_b = low_bars[1]
    prev2_b = low_bars[2]

    # breakout detection (ยอมรับภายใน 3 แท่งล่าสุด)
    up_now = crossed_above(prev_b.close, last_b.close, upper)
    dn_now = crossed_below(prev_b.close, last_b.close, lower)

    up_prev = crossed_above(prev2_b.close, prev_b.close, upper)
    dn_prev = crossed_below(prev2_b.close, prev_b.close, lower)

    up_break = up_now or up_prev
    dn_break = dn_now or dn_prev

    # default outputs
    sl_points = 250.0
    tp1_points = 500.0
    tp2_points = 1000.0

    res: Dict[str, Any] = {
        "status": "OK",
        "symbol": symbol,
        "tf_high": tf_high,
        "tf_low": tf_low,
        "box": {
            "upper": round(upper, 2),
            "lower": round(lower, 2),
            "ref_bar": hi_bar.model_dump(),
        },
        "overlay": {
            "last": last_b.model_dump(),
            "prev": prev_b.model_dump(),
        },
        "signal": None,
        "entry": None,
        "entry_50": None,
        "sl": None,
        "tp1": None,
        "tp2": None,
        "message": "",
    }

    # tolerance for retest (max 100 points or half body of latest bar)
    body = abs(last_b.close - prev_b.close)
    tol = max(100.0, 0.5 * body)

    # ========== LONG ==========
    if up_break:
        entry = upper
        mid = lower + (upper - lower) * 0.5  # จุด 50% ในกรอบ
        entry50 = (entry + mid) / 2.0        # ยอมให้ลึกถึงกึ่งระหว่าง entry กับกรอบกลาง

        if near(last_b.close, entry, tol) or near(last_b.close, entry50, tol):
            res["signal"] = "ENTRY_LONG"
            res["message"] = "Breakout ขึ้นแล้ว ราคากำลัง/เพิ่งรีเทสต์กรอบบน"
        else:
            res["signal"] = "BREAKOUT_LONG_WAIT_RETEST"
            res["message"] = "Breakout ขึ้น กำลังรอราคารีเทสต์กรอบ"

        res["entry"] = round(entry, 2)
        res["entry_50"] = round(entry50, 2)
        res["sl"] = round(entry - sl_points, 2)
        res["tp1"] = round(entry + tp1_points, 2)
        res["tp2"] = round(entry + tp2_points, 2)
        return res

    # ========== SHORT ==========
    if dn_break:
        entry = lower
        mid = lower + (upper - lower) * 0.5
        entry50 = (entry + mid) / 2.0

        if near(last_b.close, entry, tol) or near(last_b.close, entry50, tol):
            res["signal"] = "ENTRY_SHORT"
            res["message"] = "Breakout ลงแล้ว ราคากำลัง/เพิ่งรีเทสต์กรอบล่าง"
        else:
            res["signal"] = "BREAKOUT_SHORT_WAIT_RETEST"
            res["message"] = "Breakout ลง กำลังรอราคารีเทสต์กรอบ"

        res["entry"] = round(entry, 2)
        res["entry_50"] = round(entry50, 2)
        res["sl"] = round(entry + sl_points, 2)
        res["tp1"] = round(entry - tp1_points, 2)
        res["tp2"] = round(entry - tp2_points, 2)
        return res

    # ========== WAIT ==========
    res["message"] = "WAIT — รอราคาเบรกกรอบบน/ล่าง (ที่ TF ต่ำ)."
    return res


# =====================
# Routes
# =====================
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}


@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


# รุ่นเก่า: /signal (body อาจส่ง tf หรือ tf_low)
@app.post("/signal")
def signal(req: SignalRequest):
    try:
        return analyze_breakout(req.symbol, req.tf_high, req.tf_low)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ใหม่ให้ตรงกับปุ่มหน้าเว็บ: /breakout
@app.post("/breakout")
def breakout(req: SignalRequest):
    try:
        return analyze_breakout(req.symbol, req.tf_high, req.tf_low)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
