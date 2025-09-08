# XAU Scanner – FastAPI

## Run local
```bash
python -m venv .venv && source .venv/bin/activate   # Windows ใช้ .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
