from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import os, httpx
from scanner import compute_signal

app = FastAPI(title="xau-scanner", version=os.getenv("APP_VERSION", "2025-09-07.1"))

# CORS: อนุญาตเว็บ Netlify
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,   # ใส่โดเมน Netlify ก็ได้ เช่น https://your-site.netlify.app
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"app": "xau-scanner", "version": app.version, "ok": True}

class SignalRequest(BaseModel):
    symbol: str = Field(..., example="XAU/USD")
    higher_tf: str = Field(..., example="H4")
    lower_tf: str = Field(..., example="M15")
    sl_points: int = Field(..., example=250)
    tp1_points: int = Field(..., example=500)
    tp2_points: int = Field(..., example=1000)

@app.post("/signal")
async def signal(req: SignalRequest):
    # เรียก TwelveData ผ่าน scanner.py (ไม่ต้องรับรูปแล้ว)
    try:
        result = await compute_signal(
            symbol=req.symbol,
            higher_tf=req.higher_tf,
            lower_tf=req.lower_tf,
            sl_points=req.sl_points,
            tp1_points=req.tp1_points,
            tp2_points=req.tp2_points,
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

