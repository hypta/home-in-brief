# Build context is the repo root:  docker build -f jobs/kb-refresh/Dockerfile .
# Image tag must match the playwright version pinned in requirements.txt (same as rates-refresh).
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
WORKDIR /app
COPY jobs/kb-refresh/requirements.txt jobs/kb-refresh/requirements.txt
RUN pip install --no-cache-dir -r jobs/kb-refresh/requirements.txt
COPY kb/ kb/
COPY jobs/kb-refresh/ jobs/kb-refresh/
ENV DATA_BACKEND=firestore PYTHONUNBUFFERED=1
WORKDIR /app/jobs/kb-refresh
CMD ["python", "main.py"]
