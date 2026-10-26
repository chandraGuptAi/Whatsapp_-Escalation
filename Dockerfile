FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    WHATSAPP_SESSION_PATH=/service/data/whatsapp-session.sqlite3
WORKDIR /service
RUN apt-get update \
    && apt-get install -y --no-install-recommends libmagic1 fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home service
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts/migrate_sqlite_to_postgres.py ./scripts/migrate_sqlite_to_postgres.py
RUN mkdir -p data && chown -R service:service data
USER service
EXPOSE 8201
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8201/health', timeout=3)"]
CMD ["uvicorn", "app.main:build_app", "--factory", "--host", "0.0.0.0", "--port", "8201", "--workers", "1", "--no-proxy-headers"]
