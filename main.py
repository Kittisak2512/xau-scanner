
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
import io
from scanner import scan_m15_breakout_m5_confirm

app = FastAPI(title="XAUUSD Scanner – M15 Breakout + M5 Confirm")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScanBody(BaseModel):
    data: list  # list of {time,open,high,low,close,volume?}
    retest_m5_window: int = 24
    sl_after_zone: int = 12
    tp1_pts: int = 25
    tp2_pts: int = 50

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/scan")
def scan(body: ScanBody):
    df = pd.DataFrame(body.data)
    res = scan_m15_breakout_m5_confirm(
        df,
        retest_m5_window=body.retest_m5_window,
        sl_after_zone=body.sl_after_zone,
        tp1_pts=body.tp1_pts,
        tp2_pts=body.tp2_pts,
    )
    return res

@app.post("/scan_csv")
async def scan_csv(file: UploadFile = File(...),
                   retest_m5_window: int = 24,
                   sl_after_zone: int = 12,
                   tp1_pts: int = 25, tp2_pts: int = 50):
    content = await file.read()
    df = pd.read_csv(io.BytesIO(content))
    res = scan_m15_breakout_m5_confirm(
        df,
        retest_m5_window=retest_m5_window,
        sl_after_zone=sl_after_zone,
        tp1_pts=tp1_pts, tp2_pts=tp2_pts
    )
    return res
