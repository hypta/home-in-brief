# Build from repo root: docker build -f services/report-api/Dockerfile .
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/shared:/app/services/report-api
WORKDIR /app
COPY services/report-api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY shared/ /app/shared/
# app/ plus fixtures/ (REPORT_MOCK=true summaries)
COPY services/report-api/app/ /app/services/report-api/app/
COPY services/report-api/fixtures/ /app/services/report-api/fixtures/
# GEMINI_API_KEY comes from Secret Manager; RULES_API_URL is set by the deploy (KB items and sources).
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
