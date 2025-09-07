# XAU Scanner Backend (Break + Close)

Run local:
  pip install -r requirements.txt
  uvicorn main:app --reload

Deploy (Render):
  Build Command: pip install -r requirements.txt
  Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
