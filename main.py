from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from scanner import scan_breakout  # และฟังก์ชันอื่นของคุณ

app = FastAPI(title="XAU Scanner API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"ok": True, "hint": "POST /scan-image with form-data 'file'."}

@app.get("/health")
def health():
    return {"status": "ok"}

# … (endpoint /scan-image ของเดิมคงไว้) …

@app.get("/scan-breakout")
def scan_breakout_api(
    symbol: str = "XAU/USD",
    higher_tf: str = "1h",
    lower_tf: str = "5min",
    sl_points: int = 250,
    tp1_points: int = 500,
    tp2_points: int = 1000
):
    """
    ตัวอย่างเรียก:
    GET /scan-breakout?higher_tf=1h&lower_tf=5min&tp1_points=500&tp2_points=1000&sl_points=250
    """
    return scan_breakout(
        symbol=symbol,
        higher_tf=higher_tf,
        lower_tf=lower_tf,
        sl_points=sl_points,
        tp1_points=tp1_points,
        tp2_points=tp2_points
    )
