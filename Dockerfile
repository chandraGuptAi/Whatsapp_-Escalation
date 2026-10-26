FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /service
RUN apt-get update && apt-get install -y --no-install-recommends libmagic1 fonts-dejavu-core && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --create-home service
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p data && chown -R service:service data
USER service
EXPOSE 8201
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8201/health', timeout=3)"]
CMD ["uvicorn", "app.main:build_app", "--factory", "--host", "0.0.0.0", "--port", "8201", "--workers", "1", "--no-proxy-headers"]
