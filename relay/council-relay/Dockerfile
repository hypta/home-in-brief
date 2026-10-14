FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 --no-create-home relay
WORKDIR /app
COPY relay.py .
USER 10001
EXPOSE 8787
HEALTHCHECK --interval=60s --timeout=5s CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"]
CMD ["python", "relay.py"]
