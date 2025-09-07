from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse
from typing import Optional
from PIL import Image
import numpy as np
import io

app = FastAPI(title="XAU Scanner API", version="1.1.0")

@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-breakout with form-data 'm15' and 'h1h4' (or 'h4')."}

def _image_trend_score(img_bytes: bytes) -> float:
    """
    Heuristic เล็ก ๆ ให้ค่า slope จากความสว่างภาพ (กัน endpoint เงียบ)
    ไม่ได้ใช้เพื่อเทรดจริง แค่ให้รีสปอนส์มีข้อมูลคงที่
    """
    im = Image.open(io.BytesIO(img_bytes)).convert("L").resize((320, 180))
    arr = np.asarray(im, dtype=np.float32) / 255.0
    col_mean = arr.mean(axis=0)
    x = np.arange(col_mean.size, dtype=np.float32)
    x = (x - x.mean()) / (x.std() + 1e-6)
    slope = float(np.dot(x, col_mean) / (x.size))
    return slope

@app.post("/scan-breakout")
async def scan_breakout(
    m15: UploadFile = File(..., description="Lower TF image (M5/M15)"),
    h1h4: Optional[UploadFile] = File(None, description="Higher TF image (H1/H4)"),
    # alias เผื่อฝั่งหน้าเว็บส่งชื่อ h4 แทน h1h4
    h4: Optional[UploadFile] = File(None, description="Higher TF image (H4)"),
    higher_tf: str = Form("H4"),
    lower_tf: str = Form("M15"),
    sl_points: int = Form(250),
    tp1_points: int = Form(500),
    tp2_points: int = Form(1000),
):
    """
    ต้องส่งเป็น multipart/form-data:
      - m15: ไฟล์รูป TF ต่ำ (M5/M15)
      - h1h4 หรือ h4: ไฟล์รูป TF สูง (H1/H4) (ส่งมาอย่างใดอย่างหนึ่งพอ)
      - higher_tf: "H1"/"H4"
      - lower_tf: "M5"/"M15"
      - sl_points, tp1_points, tp2_points: จำนวนจุด SL/TP
    """
    try:
        m15_bytes = await m15.read()
        if h1h4 is None and h4 is None:
            return JSONResponse(
                status_code=400,
                content={"detail": "Missing higher TF image. Send field 'h1h4' or 'h4'."},
            )
        h_bytes = await (h1h4.read() if h1h4 is not None else h4.read())

        # ตัวอย่าง heuristic ง่าย ๆ เพื่อให้ได้ผลลัพธ์คงที่
        slope_low = _image_trend_score(m15_bytes)
        slope_hi = _image_trend_score(h_bytes)

        direction = "UP" if slope_low > 0 else "DOWN" if slope_low < 0 else "SIDEWAYS"

        # สมมุติ box สูง/ต่ำ จากสถิติบางอย่าง (ตัวอย่างเท่านั้น)
        h_high = round(abs(slope_hi) * 5000 + 3500, 2)
        h_low  = round(h_high - (abs(slope_low) * 300 + 120), 2)
        last   = round((h_high + h_low) / 2, 2)
        box_h  = round(max(5.0, h_high - h_low), 2)

        resp = {
            "status": "WATCH",
            "reason": "ยังไม่ Breakout โซน H1/H4",
            "ref": {
                "higher_tf": higher_tf,
                "lower_tf": lower_tf,
                "H_high": h_high,
                "H_low": h_low,
                "box_height": box_h,
                "last": last,
            },
            "params": {
                "sl_points": sl_points,
                "tp1_points": tp1_points,
                "tp2_points": tp2_points,
            },
        }

        # ตัวอย่าง logic Break+Close ทันที (ถ้า last หลุดกรอบ)
        if last > h_high:
            entry = round(h_high, 2)
            side = "LONG"
            resp["status"] = "ENTRY"
            resp["signal"] = f"ENTRY {side} @ {entry} | SL {entry - sl_points} | TP1 {entry + tp1_points} | TP2 {entry + tp2_points}"
        elif last < h_low:
            entry = round(h_low, 2)
            side = "SHORT"
            resp["status"] = "ENTRY"
            resp["signal"] = f"ENTRY {side} @ {entry} | SL {entry + sl_points} | TP1 {entry - tp1_points} | TP2 {entry - tp2_points}"
        else:
            resp["signal"] = "WAIT"

        return resp

    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"internal_error: {e}"})
