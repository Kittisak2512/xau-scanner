# main.py
# -*- coding: utf-8 -*-

from __future__ import annotations

import io
import os
import traceback
from typing import Literal, Tuple, Dict, Any

import numpy as np
from PIL import Image, ImageOps

from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


APP_NAME = "xau-scanner"
APP_VERSION = "2025-09-07.1"

# -----------------------------------------------------------------------------
# FastAPI app + CORS
# -----------------------------------------------------------------------------
app = FastAPI(title=APP_NAME, version=APP_VERSION)

# ถ้าจะล็อคโดเมน Netlify ให้แทนที่ "*" ด้วย URL ของคุณ
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # e.g. ["https://venerable-sorbet-db2690.netlify.app"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------
def _read_image(file: UploadFile) -> Image.Image:
    """Read upload to PIL.Image (RGB)."""
    raw = file.file.read()
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    return img


def _to_gray_np(img: Image.Image) -> np.ndarray:
    """Convert PIL.Image -> grayscale np.float32 [0..1]."""
    g = ImageOps.grayscale(img)
    arr = np.asarray(g, dtype=np.float32) / 255.0
    return arr


def _crop_right_side(img: np.ndarray, right_ratio: float = 0.2) -> np.ndarray:
    """เอาเฉพาะด้านขวาสุด (ใช้ดู close ล่าสุด)."""
    h, w = img.shape
    x0 = int(w * (1.0 - right_ratio))
    return img[:, x0:]


def _center_band(img: np.ndarray, width_ratio: float = 0.2) -> np.ndarray:
    """แถบตรงกลาง (ใช้เป็น baseline)."""
    h, w = img.shape
    band_w = int(w * width_ratio)
    x0 = (w - band_w) // 2
    return img[:, x0:x0 + band_w]


def _estimate_box_levels(higher_img_gray: np.ndarray) -> Tuple[float, float]:
    """
    ประมาณ 'กรอบ' H1/H4 จากรูป (แบบ heuristic)
    - เอาเฉพาะพื้นที่กึ่งกลางจอ เพื่อลด noise
    - ใช้เปอร์เซ็นไทล์ 85% / 15% เป็นขอบบน/ล่างเชิงพิกเซล (ยิ่งดำ = ต่ำ)
    คืนค่าเป็น (H_high_pix, H_low_pix) หน่วย pixel index (0=บน, h-1=ล่าง)
    """
    h, w = higher_img_gray.shape
    band = higher_img_gray[:, int(w * 0.25): int(w * 0.75)]  # โฟกัสกลางจอ
    # invert เพื่อให้ "เส้น/แท่ง" สว่างขึ้น (ภาพกราฟมักพื้นมืด แท่ง/เส้นสว่าง)
    inv = 1.0 - band

    # รวมแนวแกน x -> โปรไฟล์ตามแกน y
    prof = inv.mean(axis=1)

    # ใช้เปอร์เซ็นไทล์เป็นกรอบ (หยาบ ๆ)
    # โปรไฟล์สูง = มีวัตถุมาก (เส้น/แท่ง), เลือกระดับบน/ล่างแบบคร่าว ๆ
    top_idx = int(np.clip(np.percentile(np.arange(h), 15), 0, h - 1))
    bot_idx = int(np.clip(np.percentile(np.arange(h), 85), 0, h - 1))

    # สลับให้ top < bot
    H_high_pix = float(min(top_idx, bot_idx))
    H_low_pix = float(max(top_idx, bot_idx))
    return H_high_pix, H_low_pix


def _decide_break_close(
    higher_img_gray: np.ndarray,
    lower_img_gray: np.ndarray,
    mode: Literal["break_close"] = "break_close",
    margin_ratio: float = 0.02,
) -> Dict[str, Any]:
    """
    ตัดสิน Break + Close (แบบ heuristic):
    - ประมาณ box H_high_pix/H_low_pix จากภาพ higher TF
    - ดูค่าเฉลี่ยสว่างของ 'ขอบขวา' ของภาพ lower TF เทียบกับค่ากลาง
    - ถ้าสว่างกว่า baseline มาก -> สมมุติว่าแท่งเขียวแรง (UP)
      ในทางกลับกัน -> DOWN
    หมายเหตุ: นี่เป็น logic แบบภาพรวม (ไม่อ่านราคา) เพื่อให้มีสัญญาณใช้งานได้จริง
    """
    H_high_pix, H_low_pix = _estimate_box_levels(higher_img_gray)
    box_height = max(1.0, H_low_pix - H_high_pix)

    right = _crop_right_side(lower_img_gray, right_ratio=0.18)
    base = _center_band(lower_img_gray, width_ratio=0.18)

    # invert เพื่อเน้นแท่ง/เส้นให้มีค่าสูง
    right_inv = 1.0 - right
    base_inv = 1.0 - base

    right_val = float(right_inv.mean())
    base_val = float(base_inv.mean())
    diff = right_val - base_val

    # เกณฑ์ (ยิ่งภาพสว่างต่างจากฐานมาก -> ถือว่าแรง)
    thr = 0.06  # ปรับได้

    status = "WATCH"
    signal = "WAIT"
    direction = "SIDEWAYS"
    entry_side = None  # "LONG" | "SHORT" | None

    if mode == "break_close":
        if diff > thr:
            direction = "UP"
            signal = "ENTRY"
            status = "OK"
            entry_side = "LONG"
        elif diff < -thr:
            direction = "DOWN"
            signal = "ENTRY"
            status = "OK"
            entry_side = "SHORT"

    return {
        "status": status,
        "signal": signal,
        "reason": {
            "direction": direction,
            "score": round(diff, 6),
            "note": "Heuristic from image brightness trend.",
        },
        "box": {
            "higher_tf_box_top_pix": H_high_pix,
            "higher_tf_box_bottom_pix": H_low_pix,
            "box_height_pix": box_height,
            "margin_pix": max(1.0, box_height * margin_ratio),
        },
        "entry_side": entry_side,
    }


def _build_order_line(side: Literal["LONG", "SHORT"], sl: int, tp1: int, tp2: int) -> str:
    # เนื่องจากเราอ่าน “ราคา” จากภาพไม่ได้แน่นอน จึงระบุเฉพาะรูปแบบ
    # ผู้ใช้จะมองระดับราคาเอง (หรือเพิ่ม logic OCR แกนราคาในอนาคต)
    return f"ENTRY {side} @ <break close> | SL {sl} | TP1 {tp1} | TP2 {tp2}"


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    return {"app": APP_NAME, "version": APP_VERSION, "ok": True}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/version")
def version():
    return {"version": APP_VERSION}


@app.post("/scan-breakout")
async def scan_breakout(
    # ไฟล์ภาพ: lower TF (เช่น M15 หรือ M5) + higher TF (H1/H4)
    lower: UploadFile = File(..., description="รูปกราฟ TF ต่ำ (เช่น M15/M5)"),
    higher: UploadFile = File(..., description="รูปกราฟ TF สูง (เช่น H1/H4)"),
    # TF strings
    higher_tf: str = Form(..., description="เช่น 'H4' หรือ 'H1'"),
    lower_tf: str = Form(..., description="เช่น 'M15' หรือ 'M5'"),
    # จุด SL/TP เป็น 'points'
    sl_points: int = Form(...),
    tp1_points: int = Form(...),
    tp2_points: int = Form(...),
    # โหมด (ตอนนี้รองรับ break_close เป็นหลัก)
    mode: Literal["break_close"] = Form("break_close"),
):
    """
    วิเคราะห์ภาพ 2 TF:
    - higher (H1/H4): ใช้ประมาณกรอบ
    - lower (M5/M15): ใช้ตัดสิน break+close
    คืนค่า:
    - สถานะ, สัญญาณ, คำอธิบาย, รายละเอียดกรอบ, ข้อความคำสั่งเข้าออเดอร์
    """
    try:
        # อ่านรูป
        higher_img = _read_image(higher)
        lower_img = _read_image(lower)

        higher_g = _to_gray_np(higher_img)
        lower_g = _to_gray_np(lower_img)

        judge = _decide_break_close(higher_g, lower_g, mode=mode)

        res: Dict[str, Any] = {
            "status": judge["status"],
            "signal": judge["signal"],
            "ref": {
                "higher_tf": higher_tf,
                "lower_tf": lower_tf,
            },
            "params": {
                "sl_points": sl_points,
                "tp1_points": tp1_points,
                "tp2_points": tp2_points,
            },
            "box": judge["box"],
            "reason": judge["reason"],
        }

        side = judge.get("entry_side")
        if side in ("LONG", "SHORT"):
            res["order_line"] = _build_order_line(side, sl_points, tp1_points, tp2_points)

        return JSONResponse(res)

    except Exception as e:
        tb = traceback.format_exc(limit=2)
        return JSONResponse(
            status_code=500,
            content={
                "status": "ERROR",
                "error": str(e),
                "trace": tb,
            },
        )


# -----------------------------------------------------------------------------
# Local run
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

