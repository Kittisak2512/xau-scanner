from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import os, shutil

# ✅ ดึง logic สแกนจาก scanner.py
from scanner import scan_breakout

app = FastAPI()

# CORS: อนุญาตให้เว็บ Netlify เรียกได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],         # จะกำหนดเฉพาะโดเมน Netlify ของคุณก็ได้
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-image with form-data 'file'."}

@app.get("/health")
def health():
    return {"status": "UP"}

# ✅ ตัวนี้คือ endpoint ที่ Netlify เรียกอยู่
@app.post("/scan-image")
async def scan_image(
    file: UploadFile = File(...),
    symbol: str = "XAU/USD",       # เปลี่ยนได้ เช่น "BTC/USD"
    higher_tf: str = "1h",         # H1 หรือ H4
    lower_tf: str = "5min",        # M5 หรือ 15min → ใส่ "15min" ถ้าจะใช้ M15
    retest_window_L: int = 24,     # ค่าเริ่มต้นตามที่ตั้งไว้
    sl_points: int = 12,
    tp1_points: int = 25,
    tp2_points: int = 50
):
    """
    รับรูปผ่านฟอร์ม แล้วเรียก logic scan_breakout(...) จาก scanner.py
    ตอนนี้ยังไม่ได้วิเคราะห์ภาพจริง ใช้ภาพเป็นตัว “ทริกเกอร์” ให้สแกนข้อมูลสดจาก API
    """
    # (ออปชัน) เซฟไฟล์ชั่วคราวไว้เช็ค/ดีบัก ถ้าไม่อยากเซฟให้คอมเมนต์บล็อกนี้ทิ้งได้
    temp_path = f"temp_{file.filename}"
    try:
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception:
        pass

    try:
        # เรียกใช้กลยุทธ์ Breakout – ตัวนี้คุณเพิ่งแก้ใน scanner.py แล้ว
        result = scan_breakout(
            symbol=symbol,
            higher_tf=higher_tf,
            lower_tf=lower_tf,
            retest_window_L=retest_window_L,
            sl_points=sl_points,
            tp1_points=tp1_points,
            tp2_points=tp2_points
        )

        # เตรียมผลลัพธ์ให้หน้าเว็บ
        # รูปแบบ (ตัวอย่าง): {"status":"OK", "signal":"BUY/SELL/WATCH", "entry":..., "sl":..., "tp1":..., "tp2":...}
        return JSONResponse(content=result)

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "ERROR", "reason": str(e)}
        )
    finally:
        # ลบไฟล์ชั่วคราว
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
