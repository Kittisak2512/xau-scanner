import os
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
import requests
from datetime import datetime, timezone

APP_VERSION = "2025-09-12.2"

# ========= Config =========
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
if not TWELVEDATA_API_KEY:
    # อนุญาตให้แอพสตาร์ท แต่จะ error ตอนเรียก /structure ถ้าไม่ได้ตั้งค่า key
    pass

_ALLOWED = os.getenv("ALLOWED_ORIGINS", "*").strip()
if _ALLOWED in ("", "*"):
    ALLOW_ORIGINS = ["*"]
else:
    ALLOW_ORIGINS = [o.strip() for o in _ALLOWED.split(",") if o.strip()]

# ========= App =========
app = FastAPI(title="xau-scanner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========= Models =========
ALL_TFS = ["M5", "M15", "M30", "H1", "H4", "D1"]

class StructureRequest(BaseModel):
    symbol: str = Field(..., examples=["XAU/USD", "XAUUSD"])
    timeframes: List[str] = Field(..., examples=[["M5", "M15", "M30", "H1", "H4", "D1"]])

    @field_validator("timeframes")
    @classmethod
    def validate_tfs(cls, tfs: List[str]) -> List[str]:
        up = [t.upper() for t in tfs]
        for t in up:
            if t not in ALL_TFS:
                raise ValueError(f"Unsupported TF: {t}")
        return up


# ========= Utilities =========
def td_interval(tf: str) -> str:
    m = tf.upper()
    mapping = {
        "M5": "5min",
        "M15": "15min",
        "M30": "30min",
        "H1": "1h",
        "H4": "4h",
        "D1": "1day",
    }
    if m not in mapping:
        raise ValueError(f"Unsupported TF: {tf}")
    return mapping[m]


def fetch_series(symbol: str, tf: str, size: int = 500) -> List[Dict[str, Any]]:
    """ดึงแท่งเทียนจาก TwelveData (เรียงจากใหม่ไปเก่า)"""
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
    r = requests.get(url, params=params, timeout=25)
    try:
        data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="Upstream returned non-JSON")

    if data.get("status") == "error":
        raise HTTPException(status_code=502, detail=str(data.get("message", "API error")))
    values = data.get("values")
    if not values:
        raise HTTPException(status_code=502, detail="No data from TwelveData")
    out = []
    for v in values:
        try:
            out.append({
                "dt": v["datetime"],
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"]),
            })
        except Exception:
            # ข้ามแท่งที่พาร์สไม่ได้
            continue
    if not out:
        raise HTTPException(status_code=502, detail="Cannot parse bars")
    return out  # latest first


def local_swings(bars: List[Dict[str, Any]], k: int = 3, want: str = "high", max_points: int = 3) -> List[Dict[str, Any]]:
    """
    หา swing highs/lows แบบง่าย:
    - want == "high": bar[i].high เป็นสูงสุดเมื่อเทียบกับ i-k..i+k
    - want == "low" : bar[i].low  เป็นต่ำสุดเมื่อเทียบกับ i-k..i+k
    คืนค่าเป็นรายการระดับ + แต้มสัมผัส (touches) โดยรวม nearby levels เข้าด้วยกัน
    """
    n = len(bars)
    if n < 2*k + 1:
        return []

    # ใช้ชุดเรียงจากเก่าไปใหม่เพื่อสแกนได้ลื่น
    fwd = list(reversed(bars))  # เก่าสุด -> ใหม่สุด
    idx_levels: List[float] = []

    for i in range(k, len(fwd)-k):
        win = fwd[i-k:i+k+1]
        if want == "high":
            peak = max(b["high"] for b in win)
            if abs(fwd[i]["high"] - peak) < 1e-9:
                idx_levels.append(fwd[i]["high"])
        else:
            trough = min(b["low"] for b in win)
            if abs(fwd[i]["low"] - trough) < 1e-9:
                idx_levels.append(fwd[i]["low"])

    # รวมระดับที่อยู่ใกล้กัน (cluster)
    idx_levels.sort()
    clusters: List[List[float]] = []
    gap = max(0.2, (sum(b["high"]-b["low"] for b in fwd[-50:]) / max(1, min(50, len(fwd)))) * 0.15)  # ~15% ของ avg range ล่าสุด
    for lv in idx_levels:
        if not clusters or abs(lv - clusters[-1][-1]) > gap:
            clusters.append([lv])
        else:
            clusters[-1].append(lv)

    levels: List[float] = [round(sum(c)/len(c), 2) for c in clusters]
    # นับ touches บริเวณระดับนั้น ๆ
    res = []
    tol = gap
    for lv in levels:
        touches = 0
        for b in fwd:
            if want == "high":
                # โดนใกล้ ๆ high
                if abs(b["high"] - lv) <= tol:
                    touches += 1
            else:
                if abs(b["low"] - lv) <= tol:
                    touches += 1
        res.append({"level": round(lv, 2), "touches": touches})

    # เรียงจากใกล้ราคาปัจจุบัน (ล่าสุด) และตัดแค่ max_points จุด
    last_close = bars[0]["close"]
    res.sort(key=lambda x: abs(x["level"] - last_close))
    return res[:max_points]


def detect_order_blocks(bars: List[Dict[str, Any]], lookback: int = 120, min_move: float = 2.0) -> List[Dict[str, Any]]:
    """
    ตรวจ OB แบบง่าย:
    - หาแท่ง impulsive move: |close-open| > min_move และเป็นแท่งที่มี body ใหญ่กว่าเพื่อนใกล้ ๆ
    - โซน OB = ช่วง [min(open, close), max(open, close)] ของแท่งก่อนหน้า (base candle) 1 แท่ง
    จงใจทำแบบ conservative และคืนไม่กี่โซน
    """
    res = []
    arr = bars[:lookback]  # ล่าสุด -> ย้อนหลัง
    if len(arr) < 5:
        return res
    # ใช้ค่า min_move เป็นสัดส่วนจาก true-range เฉลี่ยช่วงสั้น หาก min_move < 1 แปลว่าเป็น factor
    if min_move < 1.0:
        recent = arr[:30]
        avg_tr = sum((b["high"] - b["low"]) for b in recent) / max(1, len(recent))
        threshold = avg_tr * (1.8)  # 1.8x ของ TR เฉลี่ย
    else:
        threshold = min_move

    # ไล่จากเก่าสุด -> ใหม่สุด เพื่อได้โซนลำดับล่าสุดท้ายรายการ
    fwd = list(reversed(arr))
    for i in range(2, len(fwd)):
        c = fwd[i]
        body = abs(c["close"] - c["open"])
        if body < threshold:
            continue
        base = fwd[i-1]
        zone_low = min(base["open"], base["close"])
        zone_high = max(base["open"], base["close"])
        ob_type = "Bullish" if c["close"] > c["open"] else "Bearish"
        # รวมโซนที่ซ้ำซ้อน
        merged = False
        for z in res:
            if (abs(z["low"] - zone_low) < 0.2) and (abs(z["high"] - zone_high) < 0.2) and z["type"] == ob_type:
                merged = True
                break
        if not merged:
            res.append({
                "type": ob_type,
                "low": round(zone_low, 2),
                "high": round(zone_high, 2),
            })
        if len(res) >= 4:
            break

    # โชว์โซนที่อยู่ใกล้ราคาล่าสุดก่อน
    last_close = bars[0]["close"]
    res.sort(key=lambda z: abs((z["low"]+z["high"])/2 - last_close))
    return res[:3]


def compute_structure_for_tf(symbol: str, tf: str) -> Dict[str, Any]:
    bars = fetch_series(symbol, tf, size=500)  # ล่าสุด -> ย้อนหลัง
    # ค่าเวลาล่าสุด
    last_dt = bars[0]["dt"]
    last = bars[0]

    # สร้างแนวต้าน/แนวรับจาก swing
    resistances = local_swings(bars, k=3, want="high", max_points=3)
    supports    = local_swings(bars, k=3, want="low",  max_points=3)

    # หา OB แบบง่าย
    order_blocks = detect_order_blocks(bars, lookback=180, min_move=0.0)  # ให้คำนวณจาก TR เฉลี่ย

    return {
        "tf": tf,
        "last_bar": {
            "dt": last_dt,
            "open": round(last["open"], 2),
            "high": round(last["high"], 2),
            "low": round(last["low"], 2),
            "close": round(last["close"], 2),
        },
        "resistance": [{"level": r["level"], "touches": r["touches"]} for r in resistances],
        "support": [{"level": s["level"], "touches": s["touches"]} for s in supports],
        "order_blocks": order_blocks,
    }


# ========= Routes =========
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": APP_VERSION, "ok": True}

@app.get("/health")
def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

@app.post("/structure")
def structure(req: StructureRequest):
    try:
        results = []
        for tf in req.timeframes:
            results.append(compute_structure_for_tf(req.symbol, tf))
        return {
            "status": "OK",
            "symbol": req.symbol,
            "results": results
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
