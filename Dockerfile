FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/bluwolfer777/KeyManagement" \
      org.opencontainers.image.title="Gestione Chiavi" \
      org.opencontainers.image.description="Gestione prestiti chiavi CPT Trevano" \
      org.opencontainers.image.licenses="GPL-3.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Zurich \
    GC_HOST=0.0.0.0 \
    GC_PORT=8080 \
    GC_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 1000 --home /app app \
    && mkdir -p /data && chown app /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

USER app
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "app.py"]
