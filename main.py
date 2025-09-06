from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict
import io
import numpy as np
from PIL import Image
import cv2

app = FastAPI(title="XAU Scanner – Backend")

# เปิด CORS ให้เว็บ PWA เรียกใช้ได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ถ้าจะล็อกให้ใส่โดเมน Netlify ของพี่แทน *
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health():
    return {"ok": True}

# ------------------------------
#  Image scanner (prototype)
# ------------------------------
def analyze_image_np(img_bgr: np.ndarray) -> Dict:
    """
    วิเคราะห์แนวโน้มแบบเร็วจากรูปกราฟด้วยเส้นขอบ/มุมเอียงของเส้น
    - ใช้ Canny + HoughLinesP หาแนวเส้นเด่น ๆ
    - สรุปมุม median เพื่อตัดสินใจ Up/Down/Sideways
    """
    if img_bgr is None or img_bgr.size == 0:
        return {"status": "ERROR", "reason": ["ภาพว่างหรืออ่านไม่ได้"]}

    # ย่อภาพลด noise
    h, w = img_bgr.shape[:2]
    scale = 1000 / max(h, w)
    if scale < 1:
        img_bgr = cv2.resize(img_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    edges = cv2.Canny(gray, 60, 150)
    lines = cv2.HoughLinesP(edges, rho=1, theta=np.pi / 180, threshold=60,
                            minLineLength=40, maxLineGap=10)

    if lines is None or len(lines) == 0:
        return {
            "status": "WATCH",
            "direction": "SIDEWAYS",
            "confidence": 0.2,
            "reason": ["ไม่พบเส้นแนวโน้มเด่นชัด (เส้นน้อย)"]
        }

    # เก็บมุมเส้น (องศา) -90..90
    angles = []
    for l in lines[:, 0, :]:
        x1, y1, x2, y2 = l
        dx, dy = (x2 - x1), (y2 - y1)
        if dx == 0:
            angle = 90.0
        else:
            angle = np.degrees(np.arctan2(-(y2 - y1), dx))  # ลบเพราะแกน y ของภาพคว่ำ
        # กรองเส้นแนวตั้ง/แนวนอนเกินไป
        if abs(angle) > 5:  # ตัดเส้นเกือบแบน
            angles.append(angle)

    if len(angles) == 0:
        return {
            "status": "WATCH",
            "direction": "SIDEWAYS",
            "confidence": 0.25,
            "reason": ["มีแต่เส้นแบนมาก จึงมองเป็นไซด์เวย์"]
        }

    median_angle = float(np.median(angles))
    spread = float(np.std(angles))
    n = len(angles)

    # ตัดสินใจทิศ
    if median_angle > 8:
        direction = "UP"
    elif median_angle < -8:
        direction = "DOWN"
    else:
        direction = "SIDEWAYS"

    # ประมาณความมั่นใจจากจำนวนเส้นและการกระจุกตัวของมุม
    conf_count = min(n / 50.0, 1.0)      # n≥50 ≈ เต็ม
    conf_spread = max(0.0, 1.0 - (spread / 25.0))  # มุมยิ่งแคบยิ่งมั่นใจ
    confidence = round(0.5 * conf_count + 0.5 * conf_spread, 2)

    return {
        "status": "SIGNAL",
        "direction": direction,     # "UP" | "DOWN" | "SIDEWAYS"
        "confidence": confidence,   # 0..1
        "lines_used": n,
        "median_angle_deg": round(median_angle, 2),
        "spread_angle_deg": round(spread, 2),
    }

@app.post("/scan_image")
async def scan_image(file: UploadFile = File(...)):
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="กรุณาอัปโหลดไฟล์รูปภาพ")

    raw = await file.read()
    try:
        pil = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(status_code=400, detail="เปิดไฟล์รูปไม่ได้")

    img_bgr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    result = analyze_image_np(img_bgr)
    return result

