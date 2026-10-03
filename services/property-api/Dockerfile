# Build from repo root: docker build -f services/property-api/Dockerfile .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/shared:/app/services/property-api
WORKDIR /app
COPY services/property-api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY shared/ /app/shared/
COPY services/property-api/app/ /app/services/property-api/app/
# LINZ_API_KEY must be provided at run time (Secret Manager on Cloud Run).
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
