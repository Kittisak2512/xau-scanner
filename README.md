Run local:
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000

Deploy Render:
Build Command:  pip install -r requirements.txt
Start Command:  uvicorn main:app --host 0.0.0.0 --port $PORT
