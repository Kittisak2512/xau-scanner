
# XAUUSD Scanner Backend (FastAPI)

## Run locally
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

- Health: `GET /health`
- JSON scan: `POST /scan` with body:
```json
{
  "data": [{"time":"2025-09-05 09:00","open":...,"high":...,"low":...,"close":...}],
  "retest_m5_window": 24,
  "sl_after_zone": 12,
  "tp1_pts": 25,
  "tp2_pts": 50
}
```
- CSV scan: `POST /scan_csv` (multipart form file `file`)
