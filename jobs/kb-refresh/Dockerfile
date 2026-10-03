# Build context is the repo root:  docker build -f jobs/kb-refresh/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
COPY jobs/kb-refresh/requirements.txt jobs/kb-refresh/requirements.txt
RUN pip install --no-cache-dir -r jobs/kb-refresh/requirements.txt
COPY kb/ kb/
COPY jobs/kb-refresh/ jobs/kb-refresh/
ENV DATA_BACKEND=firestore
WORKDIR /app/jobs/kb-refresh
CMD ["python", "main.py"]
