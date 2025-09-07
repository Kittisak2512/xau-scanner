# main.py (เฉพาะส่วน endpoint ใหม่/แก้)
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from scanner import scan_breakout_v2

app = FastAPI(title="XAU Scanner API", version="0.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-image with form-data 'file' OR POST /scan-breakout (no file required)."}

@app.post("/scan-breakout")
async def scan_breakout(
    symbol: str = Form("XAU/USD"),
    higher_tf: str = Form("1h"),
    lower_tf: str = Form("5min"),
    buffer_points: float = Form(0.20),
    tolerance_points: float = Form(0.50),
    tp1_factor: float = Form(0.5),
    tp2_factor: float = Form(1.0),
):
    res = scan_breakout_v2(
        symbol=symbol,
        higher_tf=higher_tf,
        lower_tf=lower_tf,
        buffer_points=buffer_points,
        tolerance_points=tolerance_points,
        tp1_factor=tp1_factor,
        tp2_factor=tp2_factor,
    )
    return res

