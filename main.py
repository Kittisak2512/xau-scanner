from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI()

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

@app.post("/scan-image")
async def scan_image(file: UploadFile = File(...)):
    # demo return → ปรับ logic ทีหลังได้
    return {
        "status": "WATCH",
        "reason": "ยังไม่ Breakout โซน H1/H4",
        "ref": {
            "higher_tf": "H4",
            "lower_tf": "M15",
            "H_high": 3609.86,
            "H_low": 3486.23,
            "last": 3606.64
        },
        "params": {
            "sl_points": 250,
            "tp1_points": 500,
            "tp2_points": 1000
        }
    }

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=True)

