from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from typing import Dict
import shutil
import os
import uuid

from PIL import Image
import numpy as np

APP_NAME = "XAU Scanner API"
APP_VERSION = "0.2.0-image"

app = FastAPI(title=APP_NAME, version=APP_VERSION)

# ───────────────────────── CORS ─────────────────────────
# ปรับ origins ตามโดเมน Netlify/localhost ของคุณได้
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://*.netlify.app",
    "https://xau-scanner.onrender.com",
    "*"  # ถ้าต้องการล็อกให้ปลอดภัย ให้ลบ * แล้วใส่โดเมนจริงแทน
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────── Helpers ───────────────────────
def _analyze_trend_from_image(img: Image.Image) -> Dict:
    """
    วิเคราะห์ภาพแบบ 'placeholder heuristic'
    - resize ให้เล็กลงเพื่อความเร็ว
    - แปลงเป็นโทนเทา แล้วดูความสว่างเฉลี่ยเป็นรายคอลัมน์
    - ทำ linear regression หา slope เพื่อประเมินทิศทาง
    *** นี่เป็นตัวอย่างง่าย ๆ เพื่อให้ API ใช้งานได้ก่อน
    """

    # แปลงเป็น RGB/Gray
    if img.mode != "RGB":
        img = img.convert("RGB")

    # ครอปกรอบภาพให้เหลือส่วนกราฟ (ด้านล่างตัดส่วนอินดิเคเตอร์ออกคร่าว ๆ 25%)
    w, h = img.size
    crop_box = (int(0.05 * w), int(0.08 * h), int(0.95 * w), int(0.75 * h))
    img = img.crop(crop_box)

    # ลดขนาดเพื่อความเร็ว
    target_w = 320
    scale = target_w / img.size[0]
    img = img.resize((target_w, max(32, int(img.size[1] * scale))))

    # เป็น grayscale
    gray = img.convert("L")
    arr = np.asarray(gray, dtype=np.float32) / 255.0   # 0..1

    # ความสว่างเฉลี่ยรายคอลัมน์
    col_mean = arr.mean(axis=0)
    x = np.arange(col_mean.size, dtype=np.float32)

    # linear regression: slope = cov(x,y)/var(x)
    x_mean = x.mean()
    y_mean = col_mean.mean()
    num = ((x - x_mean) * (col_mean - y_mean)).sum()
    den = ((x - x_mean) ** 2).sum() + 1e-8
    slope = float(num / den)

    # ค่าความมั่นใจง่าย ๆ จากอัตราส่วน slope ต่อ noise
    noise = float(np.std(col_mean))
    score = float(min(1.0, max(0.0, abs(slope) / (noise + 1e-6) * 2.0)))

    if slope > 0.0005:
        direction = "UP"
    elif slope < -0.0005:
        direction = "DOWN"
    else:
        direction = "SIDEWAYS"

    return {
        "direction": direction,
        "score": round(score, 3),
        "slope": round(slope, 6),
        "note": "Heuristic from image brightness trend (placeholder)."
    }

# ─────────────────────── Endpoints ──────────────────────
@app.get("/health")
def health():
    return {"ok": True, "service": APP_NAME, "version": APP_VERSION}

@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-image with form-data 'file'."}

@app.post("/scan-image")
async def scan_image(file: UploadFile = File(...)):
    # ตรวจสอบชนิดไฟล์เบื้องต้น
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="กรุณาอัปโหลดไฟล์รูปภาพเท่านั้น")

    # จำกัดขนาดไฟล์ ~10MB
    if file.size and file.size > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="ไฟล์ใหญ่เกินไป (จำกัด ~10MB)")

    # เซฟไฟล์ชั่วคราว
    ext = os.path.splitext(file.filename or "")[1] or ".png"
    temp_name = f"upload_{uuid.uuid4().hex}{ext}"
    try:
        with open(temp_name, "wb") as buf:
            shutil.copyfileobj(file.file, buf)

        # เปิดด้วย Pillow
        img = Image.open(temp_name)

        # วิเคราะห์เบื้องต้น
        result = _analyze_trend_from_image(img)

        # ตัวอย่างสัญญาณแบบง่าย (คุณปรับกลยุทธ์จริงทีหลังได้)
        signal = {
            "status": "OK",
            "signal": "BUY" if result["direction"] == "UP" else ("SELL" if result["direction"] == "DOWN" else "WAIT"),
            "reason": result,
        }
        return JSONResponse(signal)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image processing error: {e}")
    finally:
        try:
            if os.path.exists(temp_name):
                os.remove(temp_name)
        except:
            pass

@app.get("/version")
def version():
    return {"version": APP_VERSION}
