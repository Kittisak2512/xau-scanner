import io
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from PIL import Image, ImageStat

from scanner import scan_breakout

app = FastAPI(title="XAU Scanner API", version="1.1.0")

# เปิด CORS ให้เว็บ Netlify เรียกได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ถ้าจะจำกัดให้ใส่โดเมน Netlify ของคุณ
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-image with form-data 'file'."}

@app.get("/health")
def health():
    return {"status": "ok"}

# ---------- 1) สแกนภาพ (แนวโน้มเร็วจากความสว่างภาพ) ----------
@app.post("/scan-image")
async def scan_image(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        img = Image.open(io.BytesIO(raw)).convert("L")  # เป็นขาวดำ (วัดสว่าง)
        # ย่อเพื่อเร่งคำนวณ
        img = img.resize((min(800, img.width), int(img.height * min(800, img.width) / img.width)))
        w, h = img.size
        # วัดแถวล่างสุด (บริเวณกราฟ)
        strip = img.crop((0, int(h*0.65), w, h))
        stat = ImageStat.Stat(strip)
        # heuristic: ความสว่างเฉลี่ยฝั่งซ้าย/ขวา
        left  = strip.crop((0, 0, w//2, strip.height))
        right = strip.crop((w//2, 0, w, strip.height))
        lmean = ImageStat.Stat(left).mean[0]
        rmean = ImageStat.Stat(right).mean[0]
        slope = (rmean - lmean) / max(1.0, w)  # เอาความต่าง/ความกว้างเป็นสัดส่วน
        score = rmean - lmean

        if slope > 0:
            direction = "UP"
            signal = "BUY"
        elif slope < 0:
            direction = "DOWN"
            signal = "SELL"
        else:
            direction = "SIDEWAYS"
            signal = "WAIT"

        return {
            "status": "OK",
            "signal": signal if abs(slope) > 1e-4 else "WAIT",
            "reason": {
                "direction": direction if abs(slope) > 1e-4 else "SIDEWAYS",
                "score": round(score, 6),
                "slope": round(slope, 6),
                "note": "Heuristic from image brightness trend."
            }
        }
    except Exception as e:
        return JSONResponse({"status": "ERROR", "reason": str(e)}, status_code=400)

# ---------- 2) สแกน Breakout (H1/H4 -> เข้า M5/M15) ----------
@app.post("/scan-breakout")
async def scan_breakout_api(file: UploadFile = File(...)):
    # ไม่ได้ใช้ภาพในการตัดสิน breakout ที่แท้จริง (เราใช้ราคาจริง) — รับไฟล์ไว้เพื่อให้ UX เหมือนกัน
    _ = await file.read()
    # ปรับ timeframe เป้าหมายตามความต้องการ (ค่าเริ่มต้น H1 / M5)
    out = scan_breakout(
        symbol="XAU/USD",
        higher_tf="1h",     # เปลี่ยนเป็น "4h" ได้ถ้าต้องการ
        lower_tf="5min",    # หรือ "15min"
        buffer_H=120,       # ช่วงกล่อง H1/H4
        retest_window_L=24, # ดึง M5/M15 กี่แท่งหลัง
        sl_points=250,
        tp1_points=500,
        tp2_points=1000,
    )
    return out
