FROM python:3.11-slim

LABEL maintainer="Askaria"
LABEL version="1.2.0"
LABEL description="Askaria — Serveur de streaming musical"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7777

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic1 \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /askariserver

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

VOLUME ["/music", "/data"]
EXPOSE 7777

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7777/health')"

# PostgreSQL supporte plusieurs workers sans risque de corruption
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "7777", \
     "--workers", "2", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
