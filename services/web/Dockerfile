# Build from repo root: docker build -f services/web/Dockerfile .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/services/web
WORKDIR /app
COPY services/web/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/web/app/ /app/services/web/app/
# PROPERTY_API_URL and RULES_API_URL must be provided at run time.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
