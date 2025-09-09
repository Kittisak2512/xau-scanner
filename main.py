from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from scanner import scan_signal
import os

app = FastAPI()

# Allow only your Netlify frontend
ALLOWED_ORIGINS = [
    "https://venerable-sorbet-db2690.netlify.app"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {"app": "xau-scanner", "version": "2025-09-08.1", "ok": True}

@app.post("/scan-signal")
async def scan_signal_api(symbol: str, higher_tf: str, lower_tf: str, sl: int, tp1: int, tp2: int):
    api_key = os.getenv("TWELVEDATA_API_KEY", "")
    if not api_key:
        return {"error": "Missing TWELVEDATA_API_KEY"}

    result = scan_signal(symbol, higher_tf, lower_tf, sl, tp1, tp2, api_key)
    return result
