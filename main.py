# main.py
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict
from io import BytesIO

from PIL import Image
import numpy as np

app = FastAPI(title="XAU Scanner API", version="0.1.0")

# Allow all origins (so Netlify front-end can call Render freely)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Config / Params ----
DEFAULT_PARAMS = {"sl_points": 250, "tp1_points": 500, "tp2_points": 1000}


# ---- Small helpers ----
def _pil_to_gray_np(img: Image.Image) -> np.ndarray:
    """Convert PIL image to grayscale numpy array float32 [0..255]."""
    if img.mode != "L":
        img = img.convert("L")
    arr = np.asarray(img).astype("float32")
    return arr


def _read_upload_to_np(file: UploadFile) -> np.ndarray:
    """Read UploadFile -> grayscale numpy array."""
    data = file.file.read()
    img = Image.open(BytesIO(data))
    return _pil_to_gray_np(img)


def analyze_trend_brightness(img_arr: np.ndarray) -> Dict:
    """
    A very light heuristic:
      - take column-wise average brightness
      - fit a naive slope (right - left)
      - direction by slope sign
    """
    # average brightness per column
    col_mean = img_arr.mean(axis=0)
    left = float(np.mean(col_mean[: max(1, len(col_mean) // 8)]))
    right = float(np.mean(col_mean[-max(1, len(col_mean) // 8) :]))
    slope = (right - left) / 255.0  # normalize

    # score ~ small magnitude slope
    score = float(slope)

    if slope > 0.002:
        direction = "UP"
    elif slope < -0.002:
        direction = "DOWN"
    else:
        direction = "SIDEWAYS"

    return {
        "direction": direction,
        "score": round(score, 6),
        "slope": round(slope, 6),
        "note": "Heuristic from image brightness trend.",
    }


def get_box_levels(higher_img: np.ndarray) -> Dict[str, float]:
    """
    Estimate a 'box' from higher timeframe image using brightness distribution.
    - We use row-wise mean brightness, then take two quantiles to define top/bottom band.
    - This is NOT real price; it's a stable heuristic to compare with 'last'.
    Returned values are in 'pseudo-price space' (pixels mapped directly).
    """
    row_mean = higher_img.mean(axis=1)  # per row
    # y-axis: top=0, bottom=max. We want two boundaries; use quantiles to be robust
    q_low = np.quantile(row_mean, 0.25)
    q_high = np.quantile(row_mean, 0.75)

    # representative row indices near those quantiles
    low_rows = np.where(row_mean <= q_low)[0]
    high_rows = np.where(row_mean >= q_high)[0]

    if len(low_rows) == 0:
        low_rows = np.array([int(0.7 * len(row_mean))])
    if len(high_rows) == 0:
        high_rows = np.array([int(0.3 * len(row_mean))])

    # map rows to pseudo prices: invert y so "top has higher price"
    H = float(higher_img.shape[0])
    H_high = float(H - np.mean(high_rows))  # upper bound
    H_low = float(H - np.mean(low_rows))    # lower bound

    # ensure high > low
    if H_high < H_low:
        H_high, H_low = H_low, H_high

    return {"H_high": round(H_high, 2), "H_low": round(H_low, 2)}


def read_last_from_lower(lower_img: np.ndarray) -> float:
    """
    Estimate the 'last' from lower TF image:
    - take rightmost ~4% columns, compute the vertical COM (weighted by brightness)
    - map to pseudo price (invert y)
    """
    w = lower_img.shape[1]
    take = max(1, int(w * 0.04))
    roi = lower_img[:, -take:]

    # weight by brightness
    weights = roi.astype("float32")
    rows = np.arange(roi.shape[0], dtype="float32").reshape(-1, 1)
    # prevent division by zero
    s = weights.sum()
    if s <= 1e-6:
        y = float(roi.shape[0] // 2)
    else:
        y = float((rows * weights).sum() / s)

    H = float(lower_img.shape[0])
    last = float(H - y)  # invert to pseudo price
    return round(last, 2)


def decide_breakout_action(H_high: float, H_low: float, last: float) -> Dict:
    """
    Decide status / entry based on breakout & retest rules.
    - Inside box => WATCH
    - Break above/below:
        - If 'near' boundary => ENTRY (immediate)
        - Else => BREAKED_WAIT_RETEST
    'Near' threshold = min(25% of box height, 80)
    """
    box_height = max(1.0, H_high - H_low)
    tol = min(box_height * 0.25, 80.0)

    ref = {
        "H_high": H_high,
        "H_low": H_low,
        "last": last,
        "params": DEFAULT_PARAMS.copy(),
    }

    # Inside / touching box
    if (H_low - tol) <= last <= (H_high + tol):
        return {
            "status": "WATCH",
            "reason": "ยังไม่ Breakout โซน H4/M15",
            "ref": ref,
        }

    # Breakout up
    if last > H_high + tol:
        # retest zone?
        if (H_high) < last <= (H_high + tol * 2):
            # Entry long at boundary
            entry = round(H_high, 2)
            sl = round(entry - DEFAULT_PARAMS["sl_points"], 2)
            tp1 = round(entry + DEFAULT_PARAMS["tp1_points"], 2)
            tp2 = round(entry + DEFAULT_PARAMS["tp2_points"], 2)
            return {
                "status": "ENTRY",
                "side": "BUY",
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "ref": ref,
            }
        else:
            return {
                "status": "BREAKED_WAIT_RETEST",
                "reason": "เบรกขึ้นไกลไป รอรีเทสก่อน",
                "ref": ref,
            }

    # Breakout down
    if last < H_low - tol:
        if (H_low - tol * 2) <= last < (H_low):
            entry = round(H_low, 2)
            sl = round(entry + DEFAULT_PARAMS["sl_points"], 2)
            tp1 = round(entry - DEFAULT_PARAMS["tp1_points"], 2)
            tp2 = round(entry - DEFAULT_PARAMS["tp2_points"], 2)
            return {
                "status": "ENTRY",
                "side": "SELL",
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "ref": ref,
            }
        else:
            return {
                "status": "BREAKED_WAIT_RETEST",
                "reason": "เบรกลงไกลไป รอรีเทสก่อน",
                "ref": ref,
            }

    # fallback
    return {
        "status": "WATCH",
        "reason": "ยังไม่ Breakout",
        "ref": ref,
    }


# ---- Models (for docs only) ----
class HealthOut(BaseModel):
    ok: bool
    hint: str


# ---- Routes ----
@app.get("/", response_model=HealthOut)
def root():
    return {
        "ok": True,
        "hint": "POST /scan-image (form-data 'file'), POST /scan-breakout (form-data 'm15','h4', optional 'higher_tf','lower_tf')",
    }


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/scan-image")
async def scan_image(file: UploadFile = File(...)):
    """
    Quick trend scan from a single chart image.
    """
    arr = _read_upload_to_np(file)
    reason = analyze_trend_brightness(arr)
    return {
        "status": "OK",
        "signal": "WAIT",
        "reason": reason,
    }


@app.post("/scan-breakout")
async def scan_breakout(
    m15: UploadFile = File(..., description="lower timeframe image (M5/M15)"),
    h4: UploadFile = File(..., description="higher timeframe image (H1/H4)"),
    higher_tf: str = Form("H4"),
    lower_tf: str = Form("M15"),
):
    """
    Breakout + Retest logic using two images:
      - higher_tf image (H1/H4) to build box (H_high/H_low)
      - lower_tf image (M5/M15) to estimate 'last'

    Returns WATCH / ENTRY / BREAKED_WAIT_RETEST
    """
    higher_img = _read_upload_to_np(h4)
    lower_img = _read_upload_to_np(m15)

    box = get_box_levels(higher_img)
    H_high, H_low = box["H_high"], box["H_low"]
    last = read_last_from_lower(lower_img)

    decision = decide_breakout_action(H_high, H_low, last)
    # decorate reference
    decision["ref"]["higher_tf"] = higher_tf
    decision["ref"]["lower_tf"] = lower_tf
    return decision
