from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
import shutil
import os

app = FastAPI()

@app.post("/scan-image")
async def scan_image(file: UploadFile = File(...)):
    # บันทึกไฟล์ลงชั่วคราว
    file_location = f"temp_{file.filename}"
    with open(file_location, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # 👇 ตรงนี้คือ logic ที่คุณต้องเขียนเพิ่ม เช่นส่งเข้า scanner.py
    # ตอนนี้ขอให้ตอบกลับแบบ mock ก่อน
    result = {
        "status": "OK",
        "signal": "BUY",
        "entry": 2000,
        "sl": 1990,
        "tp1": 2020,
        "tp2": 2050
    }

    # ลบไฟล์ออก
    os.remove(file_location)

    return JSONResponse(content=result)
