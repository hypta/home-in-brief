# Build context: repo root.  docker build -f jobs/rates-refresh/Dockerfile .
# Image tag must match the playwright version pinned in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
COPY jobs/rates-refresh/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY jobs/rates-refresh/ /app/

ENV DATA_BACKEND=firestore PYTHONUNBUFFERED=1
CMD ["python", "-m", "rates_refresh"]
