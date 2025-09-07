from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import Optional
from io import BytesIO
from PIL import Image
import numpy as np
import uvicorn

app = FastAPI(title="XAU Scanner API", version="0.2.0")

# อนุญาต CORS ให้หน้า Netlify เรียกได้
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # จะระบุโดเมน *.netlify.app / onrender.com ก็ได้
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _to_gray(img: Image.Image) -> np.ndarray:
    if img.mode != "L":
        img = img.convert("L")
    return np.array(img, dtype=np.float32) / 255.0

def _read_upload(file: UploadFile) -> Image.Image:
    data = BytesIO(file.file.read())
    return Image.open(data)

@app.get("/")
def root():
    return {
        "ok": True,
        "hint": "POST /scan-image with form-data 'file'. "
                "POST /scan-breakout with files 'm15' and 'h4' (or 'file_m15','file_h4')."
    }

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/scan-image")
async def scan_image(file: UploadFile = File(...)):
    """
    วิเคราะห์ภาพเดี่ยวแบบ heuristic (เอียงสว่างซ้าย->ขวาให้สัญญาณ)
    """
    try:
        img = _read_upload(file)
        g = _to_gray(img)            # (H, W)
        col_mean = g.mean(axis=0)    # ความสว่างเฉลี่ยตามแกน x

        # linear slope โดยประมาณ (ต่างค่าเฉลี่ยต้น-ปลาย)
        slope = float(col_mean[-1] - col_mean[0])
        score = abs(slope)

        if slope > 0.003:
            signal = "BUY"
            direction = "UP"
        elif slope < -0.003:
            signal = "SELL"
            direction = "DOWN"
        else:
            signal = "WAIT"
            direction = "SIDEWAYS"

        return JSONResponse({
            "status": "OK",
            "signal": signal,
            "reason": {
                "direction": direction,
                "score": round(score, 6),
                "slope": round(slope, 6),
                "note": "Heuristic from image brightness trend."
            }
        })
    except Exception as e:
        return JSONResponse({"status": "ERROR", "detail": str(e)}, status_code=500)

def _get_bounds_from_higher_tf(img_h4: Image.Image):
    g = _to_gray(img_h4)
    # ใช้เปอร์เซ็นไทล์เป็นแนวกล่องบน/ล่างแบบคร่าว ๆ
    h_high = float(np.quantile(g, 0.95) * 10000)  # scale ให้เป็นเลข ~ หลักพัน
    h_low  = float(np.quantile(g, 0.05) * 10000)
    last   = float(np.median(g[:, -5:]) * 10000)  # ค่าล่าสุด (ท้ายภาพ)
    return h_high, h_low, last

@app.post("/scan-breakout")
async def scan_breakout(
    # ยอมรับได้หลายชื่อ เพื่อกันพลาดจากฟรอนต์เดิม
    m15: Optional[UploadFile] = File(None),
    h4:  Optional[UploadFile] = File(None),
    file_m15: Optional[UploadFile] = File(None),
    file_h4:  Optional[UploadFile] = File(None),
    higher_tf: str = Form("H4"),
    lower_tf: str = Form("M15"),
    sl_points: int = Form(250),
    tp1_points: int = Form(500),
    tp2_points: int = Form(1000),
):
    """
    ตรวจ breakout ด้วย 2 ภาพ: lower TF (M5/M15) + higher TF (H1/H4)
    ฟิลด์ไฟล์: 'm15' และ 'h4' (หรือ 'file_m15','file_h4')
    """
    try:
        _m15 = m15 or file_m15
        _h4  = h4  or file_h4
        if not _m15 or not _h4:
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        img_m15 = _read_upload(_m15)
        img_h4  = _read_upload(_h4)

        # หาแนวบน/ล่างจาก H1/H4
        H_high, H_low, last_val = _get_bounds_from_higher_tf(img_h4)

        # ดูค่าเฉลี่ยท้ายภาพของ M15 เพื่อเช็คว่าทะลุหรือยัง
        gm = _to_gray(img_m15)
        m_last = float(np.median(gm[:, -5:]) * 10000)

        status = "WATCH"
        reason = f"ยังไม่ Breakout โซน {higher_tf}/{lower_tf}"
        levels = None

        # ถ้าทะลุบน/ล่าง แบบง่าย ๆ
        if m_last > H_high:
            status = "BREAKOUT_UP"
            reason = f"เบรกกรอบบนจาก {higher_tf}"
            levels = {
                "entry": round(m_last, 2),
                "sl": round(m_last - sl_points, 2),
                "tp1": round(m_last + tp1_points, 2),
                "tp2": round(m_last + tp2_points, 2)
            }
        elif m_last < H_low:
            status = "BREAKOUT_DOWN"
            reason = f"เบรกกรอบล่างจาก {higher_tf}"
            levels = {
                "entry": round(m_last, 2),
                "sl": round(m_last + sl_points, 2),
                "tp1": round(m_last - tp1_points, 2),
                "tp2": round(m_last - tp2_points, 2)
            }

        return JSONResponse({
            "status": status,
            "reason": reason,
            "ref": {
                "higher_tf": higher_tf,
                "lower_tf": lower_tf,
                "H_high": round(H_high, 2),
                "H_low":  round(H_low,  2),
                "last":   round(last_val, 2)
            },
            "params": {
                "sl_points": sl_points,
                "tp1_points": tp1_points,
                "tp2_points": tp2_points
            },
            **({"levels": levels} if levels else {})
        })
    except Exception as e:
        return JSONResponse({"status": "ERROR", "detail": str(e)}, status_code=500)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
