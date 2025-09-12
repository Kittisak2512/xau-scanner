# ==== เพิ่มด้านบนไฟล์เดิม (import อยู่แล้วใช้งานร่วมกับของเดิม) ====
from typing import Tuple

# ---- pivots ----
def find_pivots(bars: List[Candle], left: int = 2, right: int = 2) -> Dict[str, List[Dict[str, Any]]]:
    highs, lows = [], []
    n = len(bars)
    # bars ลำดับล่าสุดก่อน: [0] ใหม่สุด → อยากให้เรียงเก่า→ใหม่เพื่อหาพลวัตรง่าย
    seq = list(reversed(bars))  # เก่า -> ใหม่
    for i in range(left, n - right):
        win = seq[i - left:i + right + 1]
        c = seq[i]
        if all(c.high >= w.high for w in win) and any(c.high > w.high for w in win if w != c):
            highs.append({"i": i, "dt": c.dt, "price": c.high})
        if all(c.low <= w.low for w in win) and any(c.low < w.low for w in win if w != c):
            lows.append({"i": i, "dt": c.dt, "price": c.low})
    return {"highs": highs, "lows": lows}

# ---- cluster levels (R/S) ----
def cluster_levels(points: List[float], band: float, min_hits: int = 3, max_levels: int = 10) -> List[Dict[str, Any]]:
    if not points:
        return []
    points = sorted(points)
    clusters = []
    cur = [points[0]]
    for p in points[1:]:
        if abs(p - cur[-1]) <= band:
            cur.append(p)
        else:
            clusters.append(cur)
            cur = [p]
    clusters.append(cur)
    # สร้างระดับจากค่าเฉลี่ยคลัสเตอร์
    lvls = []
    for c in clusters:
        if len(c) >= min_hits:
            lvls.append({"price": round(sum(c)/len(c), 2), "hits": len(c)})
    # เลือก top by hits
    lvls.sort(key=lambda x: (-x["hits"], x["price"]))
    return lvls[:max_levels]

# ---- trendlines (RANSAC ง่าย) ----
def fit_line(p1: Tuple[int, float], p2: Tuple[int, float]) -> Tuple[float, float]:
    # กลับมาเป็น y = a*x + b (x = index เวลาเก่า->ใหม่)
    (x1, y1), (x2, y2) = p1, p2
    if x2 == x1:
        x2 += 1e-6
    a = (y2 - y1) / (x2 - x1)
    b = y1 - a * x1
    return a, b

def line_error(a: float, b: float, pts: List[Tuple[int, float]]) -> List[float]:
    return [abs((a*x + b) - y) for x, y in pts]

def detect_trendlines(pivots: List[Dict[str, Any]], tol: float, min_touches: int = 3, max_lines: int = 3) -> List[Dict[str, Any]]:
    # pivots: [{"i": idx, "price": ...}]
    if len(pivots) < min_touches:
        return []
    pts = [(p["i"], p["price"]) for p in pivots]
    n = len(pts)
    used = [False]*n
    lines = []
    # ลองทุกคู่ (O(n^2) สำหรับชุดสวิงที่คัดแล้วจะไม่เยอะ)
    for i in range(n):
        for j in range(i+1, n):
            a, b = fit_line(pts[i], pts[j])
            errs = line_error(a, b, pts)
            inliers = [k for k,e in enumerate(errs) if e <= tol]
            if len(inliers) >= min_touches:
                xs = [pts[k][0] for k in inliers]
                ys = [pts[k][1] for k in inliers]
                line = {
                    "a": a, "b": b,
                    "touches": len(inliers),
                    "x_min": min(xs), "x_max": max(xs),
                    "y_min": round(min(ys),2), "y_max": round(max(ys),2)
                }
                # กันซ้ำแบบง่าย: ถ้ามีเส้น a,b ใกล้กันมากแล้ว ข้าม
                dup = any(abs(a - L["a"]) < 1e-5 and abs(b - L["b"]) < 20 for L in lines)
                if not dup:
                    lines.append(line)
    lines.sort(key=lambda L: (-L["touches"], L["x_max"]-L["x_min"]))
    return lines[:max_lines]

# ---- order block (OB) เบื้องต้นและชัดเจน ----
def detect_order_blocks(bars: List[Candle], piv: Dict[str, List[Dict[str, Any]]], lookback: int = 100) -> List[Dict[str, Any]]:
    # เกณฑ์อย่างง่าย: หา "แท่งสุดท้ายของขาขึ้น" ก่อนราคาทุบทะลุ swing low สำคัญ → OB ขาลง
    # และ "แท่งสุดท้ายของขาลง" ก่อนราคาดันทะลุ swing high สำคัญ → OB ขาขึ้น
    # ใช้เฉพาะ 100 แท่งล่าสุด (เพื่อความเร็ว/ความเกี่ยวข้อง)
    seq = list(reversed(bars))[:lookback]  # เก่า->ใหม่ (จำกัด lookback)
    highs = piv["highs"]; lows = piv["lows"]
    ob_list = []

    # สวิงสำคัญ: เอา top-N ตามความสดใหม่
    key_highs = sorted(highs, key=lambda x: x["i"], reverse=True)[:5]
    key_lows  = sorted(lows,  key=lambda x: x["i"], reverse=True)[:5]

    # หา BoS ลง: ทะลุ low สำคัญล่าสุด → OB = แท่งเขียวสุดท้ายก่อนทะลุ
    for kl in key_lows:
        lvl = kl["price"]
        # หาแท่งที่ close < lvl แบบมีนัย
        bos_idx = None
        for i, c in enumerate(seq):
            if c.close < lvl:
                bos_idx = i
                break
        if bos_idx is not None:
            # ย้อนกลับหา "last up candle" ก่อน bos_idx
            last_up_idx = None
            for j in range(bos_idx-1, max(bos_idx-30, -1), -1):
                if seq[j].close > seq[j].open:
                    last_up_idx = j
                    break
            if last_up_idx is not None:
                oc = seq[last_up_idx]
                ob_list.append({
                    "type": "bearish",
                    "dt": oc.dt,
                    "zone": [round(min(oc.open, oc.close),2), round(max(oc.open, oc.close),2)]
                })
                break  # เอาอันล่าสุดพอ

    # หา BoS ขึ้น: ทะลุ high สำคัญล่าสุด → OB = แท่งแดงสุดท้ายก่อนทะลุ
    for kh in key_highs:
        lvl = kh["price"]
        bos_idx = None
        for i, c in enumerate(seq):
            if c.close > lvl:
                bos_idx = i
                break
        if bos_idx is not None:
            last_dn_idx = None
            for j in range(bos_idx-1, max(bos_idx-30, -1), -1):
                if seq[j].close < seq[j].open:
                    last_dn_idx = j
                    break
            if last_dn_idx is not None:
                oc = seq[last_dn_idx]
                ob_list.append({
                    "type": "bullish",
                    "dt": oc.dt,
                    "zone": [round(min(oc.open, oc.close),2), round(max(oc.open, oc.close),2)]
                })
                break

    return ob_list

# ---- บรรจุผลโครงสร้างต่อ TF ----
def analyze_structure_for_tf(symbol: str, tf: str) -> Dict[str, Any]:
    bars = fetch_series(symbol, tf, 300)  # 300 แท่งพอ
    piv = find_pivots(bars, left=2, right=2)
    # R/S band ตาม TF (หยาบน้อยลงเมื่อ TF ใหญ่)
    band = 30.0 if tf in ("M5","M15") else (60.0 if tf=="H1" else 120.0)
    levels_high = cluster_levels([p["price"] for p in piv["highs"]], band=band)
    levels_low  = cluster_levels([p["price"] for p in piv["lows"]],  band=band)
    # Trendlines
    tol = 40.0 if tf in ("M5","M15") else (70.0 if tf=="H1" else 120.0)
    tl_down = detect_trendlines(piv["highs"], tol=tol)  # แนวต้าน (ลากจาก highs)
    tl_up   = detect_trendlines(piv["lows"],  tol=tol)  # แนวรับ (ลากจาก lows)
    # OB
    obs = detect_order_blocks(bars, piv)

    return {
        "tf": tf,
        "levels": {
            "resistance": levels_high,  # [{price,hits}]
            "support": levels_low
        },
        "trendlines": {
            "down": tl_down,  # [{a,b,touches,x_min,x_max,y_min,y_max}]
            "up": tl_up
        },
        "order_blocks": obs,  # [{type,dt,zone:[low,high]}]
        "meta": {
            "last_bar": bars[0].model_dump()
        }
    }

# ---- เอ็นด์พอยต์ใหม่: สแกนโครงสร้าง ไม่สร้างซิกเข้าออเดอร์ ----
class StructureRequest(BaseModel):
    symbol: str = Field(..., examples=["XAU/USD"])
    tfs: List[str] = Field(default_factory=lambda: ["M5","M15","H1","H4"])

    @field_validator("tfs")
    @classmethod
    def _vtfs(cls, v: List[str]) -> List[str]:
        allowed = {"M5","M15","H1","H4"}
        vv = [x.upper() for x in v]
        for t in vv:
            if t not in allowed:
                raise ValueError(f"Unsupported TF: {t}")
        return vv

@app.post("/analyze-structure")
def analyze_structure(req: StructureRequest):
    try:
        out = {"status":"OK","symbol":req.symbol,"result":[]}
        for tf in req.tfs:
            out["result"].append(analyze_structure_for_tf(req.symbol, tf))
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
