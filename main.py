from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os

app = FastAPI()

# ===== CORS =====
origins = []
allowed_origins = os.getenv("ALLOWED_ORIGINS")
if allowed_origins:
    origins = [allowed_origins]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== Models =====
class SignalRequest(BaseModel):
    symbol: str
    tf_high: str
    tf_low: str
    sl: int
    tp1: int
    tp2: int

# ===== Routes =====
@app.get("/")
def root():
    return {"app": "xau-scanner", "version": "2025-09-09.2", "ok": True}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/signal")
def get_signal(req: SignalRequest):
    # Mock logic -> เอาไปคำนวณจริงได้ทีหลัง
    return {
        "symbol": req.symbol,
        "tf_high": req.tf_high,
        "tf_low": req.tf_low,
        "sl": req.sl,
        "tp1": req.tp1,
        "tp2": req.tp2,
        "signal": "BUY" if req.sl < req.tp1 else "SELL"
    }
