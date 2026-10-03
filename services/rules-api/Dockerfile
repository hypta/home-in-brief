# Build from repo root: docker build -f services/rules-api/Dockerfile .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/shared:/app/services/rules-api
WORKDIR /app
COPY services/rules-api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY shared/ /app/shared/
COPY services/rules-api/ /app/services/rules-api/
# seed data for DATA_BACKEND=local (kb/seed and data/rates_seed.json)
COPY data/ /app/data/
COPY kb/ /app/kb/
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
