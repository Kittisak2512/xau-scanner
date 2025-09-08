# main.py
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
import os

APP_NAME = "xau-scanner"
VERSION = datetime.utcnow().strftime("%Y-%m-%d.%H%M")

# ========= CORS =========
# ใช้ ENV ถ้ามี ไม่มีก็ใช้ค่า Netlify ของคุณ
_default_origin = "https://venerable-sorbet-db2690.netlify.app"
_env_origins = os.getenv("ALLOWED_ORIGINS", _default_origin)
# อนุญาตให้คั่นด้วย comma หลายโดเมนได้เช่น "https://a.app,https://b.app"
ALLOWED_ORIGINS: List[str] = [o.strip() for o in _env_origins.split(",") if o.strip()]

app = FastAPI(title=APP_NAME, version=VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========= Models =========
class ScanParams(BaseModel):
    # สำหรับโหมดสแกนพารามิเตอร์ (ไม่ใช่อัปโหลดรูป)
    symbol: str = Field(..., example="XAU/USD")
    higher_tf: str = Field(..., example="H4", description="TF สูง (H1/H4)")
    lower_tf: str = Field(..., example="M15", description="TF ต่ำ (M5/M15)")
    sl_points: int = Field(..., example=250)
    tp1_points: int = Field(..., example=500)
    tp2_points: int = Field(..., example=1000)

class SignalOutput(BaseModel):
    status: str
    reason: str
    ref: dict
    params: dict
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    message: Optional[str] = None

# ========= Utilities (จำลองตรรกะง่ายๆ ให้ดีพลอยได้เบาๆ) =========
def _mock_box_from_higher_tf(img_bytes: bytes) -> dict:
    """
    จำลองอ่านภาพ H1/H4 → คืนค่า H_high/H_low โดยไม่หนักเครื่อง
    (ของจริงคุณอาจใช้ OpenCV/Tesseract ได้ แต่เพื่อความเบาในการดีพลอย
     และแก้ปัญหา build จึงทำ mock ไว้ให้ก่อน)
    """
    size = len(img_bytes) if img_bytes else 0
    # สุ่มค่าจำลองแบบ deterministic จากขนาดไฟล์
    base = (size % 1000) + 3450
    return {
        "H_high": round(base + 160.0, 2),
        "H_low": round(base - 160.0, 2),
        "last": round(base + 15.0, 2),
    }

def _decide_breakout(ref: dict, sl_points: int, tp1_points: int, tp2_points: int) -> SignalOutput:
    """
    ตรรกะ breakout แบบ ‘Break + Close ทันที’
    - ถ้า last > H_high → LONG
    - ถ้า last < H_low  → SHORT
    - อย่างอื่น → WATCH
    """
    Hh = ref["H_high"]
    Hl = ref["H_low"]
    last = ref["last"]

    out = SignalOutput(
        status="WATCH",
        reason="ยังไม่ Breakout โซน H1/H4",
        ref={"higher_tf": "H4", "lower_tf": "M15", "H_high": Hh, "H_low": Hl, "last": last},
        params={"sl_points": sl_points, "tp1_points": tp1_points, "tp2_points": tp2_points},
    )

    if last > Hh:
        out.status = "ENTRY_LONG"
        out.reason = "Break + Close เหนือกรอบบน (H_high)"
        out.entry = last
        out.sl = round(last - sl_points, 2)
        out.tp1 = round(last + tp1_points, 2)
        out.tp2 = round(last + tp2_points, 2)
        out.message = f"ENTRY LONG @ {out.entry} | SL {out.sl} | TP1 {out.tp1} | TP2 {out.tp2}"
    elif last < Hl:
        out.status = "ENTRY_SHORT"
        out.reason = "Break + Close ใต้กรอบล่าง (H_low)"
        out.entry = last
        out.sl = round(last + sl_points, 2)
        out.tp1 = round(last - tp1_points, 2)
        out.tp2 = round(last - tp2_points, 2)
        out.message = f"ENTRY SHORT @ {out.entry} | SL {out.sl} | TP1 {out.tp1} | TP2 {out.tp2}"

    return out

# ========= Endpoints =========
@app.get("/", tags=["meta"])
def root():
    return {"app": APP_NAME, "version": VERSION, "ok": True, "allowed_origins": ALLOWED_ORIGINS}

@app.get("/health", tags=["meta"])
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

# ---- (A) โหมดอัปโหลดรูป (multipart) ----
@app.post("/scan-breakout", response_model=SignalOutput, tags=["scan"])
async def scan_breakout(
    m15: UploadFile = File(..., description="ภาพกราฟ TF ต่ำ (M5/M15)"),
    h4: UploadFile = File(..., description="ภาพกราฟ TF สูง (H1/H4)"),
    higher_tf: str = Form(..., description="เช่น H4 หรือ H1"),
    lower_tf: str = Form(..., description="เช่น M15 หรือ M5"),
    sl_points: int = Form(..., description="เช่น 250"),
    tp1_points: int = Form(..., description="เช่น 500"),
    tp2_points: int = Form(..., description="เช่น 1000"),
):
    """
    รับรูป 2 ใบ + พารามิเตอร์ → วิเคราะห์ Break + Close
    หมายเหตุ: ตรงนี้ใช้ mock อ่านกรอบจากภาพ H4/H1 (ไม่ใช้ lib หนัก)
    """
    try:
        # อ่าน bytes (ไม่บันทึกไฟล์ลงดิสก์)
        h4_bytes = await h4.read()
        # m15_bytes = await m15.read()  # ถ้าต้องใช้เพิ่ม ให้เปิดบรรทัดนี้

        # สกัดโซนจาก H4/H1 (mock)
        ref = _mock_box_from_higher_tf(h4_bytes)
        # วิเคราะห์ breakout
        out = _decide_breakout(ref, sl_points, tp1_points, tp2_points)
        # เขียน TF ที่ผู้ใช้เลือกลงผลลัพธ์
        out.ref["higher_tf"] = higher_tf
        out.ref["lower_tf"] = lower_tf
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"scan-breakout error: {e}")

# ---- (B) โหมดส่งพารามิเตอร์ (JSON) ----
@app.post("/scan-signal", response_model=SignalOutput, tags=["scan"])
def scan_signal(params: ScanParams):
    """
    โหมดสแกนที่ไม่ต้องอัปโหลดรูป: front ส่ง JSON -> backend ตัดสินใจให้
    ตอนนี้จำลองค่า H_high/H_low/last เฉย ๆ เพื่อให้ดีพลอยง่ายและตอบกลับได้ครบ
    """
    # จำลอง last/H_high/H_low ให้สัมพันธ์กัน (ปรับปรุงได้ภายหลัง)
    base = 3500.0
    ref = {
        "H_high": round(base + 110, 2),
        "H_low": round(base - 110, 2),
        "last": round(base + 20, 2),
    }
    out = _decide_breakout(ref, params.sl_points, params.tp1_points, params.tp2_points)
    out.ref["higher_tf"] = params.higher_tf
    out.ref["lower_tf"] = params.lower_tf
    out.ref["symbol"] = params.symbol
    return out
