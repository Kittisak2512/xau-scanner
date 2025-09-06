# main.py
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from scanner import scan_breakout, scan_image  # ให้มีฟังก์ชัน 2 ตัวใน scanner.py

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-image with form-data 'file'.  POST /scan-breakout with form-data 'file'."}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/scan-image")
async def scan_image_api(file: UploadFile = File(...)):
    img = await file.read()
    return scan_image(img)

@app.post("/scan-breakout")
async def scan_breakout_api(file: UploadFile = File(...)):
    img = await file.read()
    return scan_breakout(img)
