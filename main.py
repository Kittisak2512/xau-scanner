from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI()

# ✅ เปิดให้ Netlify + Localhost เรียก API ได้
origins = [
    "https://venerable-sorbet-db2690.netlify.app",  # Netlify frontend
    "http://localhost:3000",  # local dev
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Routes
# =========================

@app.get("/")
def read_root():
    return {"status": "OK", "msg": "Backend is running!"}

# ✅ ตัวอย่าง endpoint สำหรับ Breakout Scanner
@app.post("/scan-breakout")
async def scan_breakout(
    m15: UploadFile = File(...),
    h4: UploadFile = File(...),
    sl_points: int = Form(...),
    tp1_points: int = Form(...),
    tp2_points: int = Form(...),
    higher_tf: str = Form(...),
    lower_tf: str = Form(...)
):
    """
    รับไฟล์กราฟ 2 TF (M15 + H1/H4) และพารามิเตอร์ SL, TP1, TP2
    คืนค่าเป็นข้อความพร้อมจุดเข้า/SL/TP
    """

    # 📌 สมมติว่าตรวจเจอ Breakout แล้ว (ตัวอย่าง logic เฉย ๆ)
    # คุณสามารถใส่เงื่อนไขจริงได้ทีหลัง
    entry_price = 3500.00
    sl_price = entry_price - sl_points * 0.1
    tp1_price = entry_price + tp1_points * 0.1
    tp2_price = entry_price + tp2_points * 0.1

    return {
        "status": "ENTRY",
        "signal": "LONG",
        "message": f"ENTRY LONG @ {entry_price} | SL {sl_price} | TP1 {tp1_price} | TP2 {tp2_price}",
        "ref": {
            "higher_tf": higher_tf,
            "lower_tf": lower_tf
        }
    }


# =========================
# Run local
# =========================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
