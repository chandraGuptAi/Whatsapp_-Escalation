# One image for every process; compose picks the command (api, report worker, scheduler, ingest).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /service

# Dependencies first (cached layer), taken straight from pyproject.toml.
COPY pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(p['dependencies'] + p['optional-dependencies']['whatsapp']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt
COPY app ./app
COPY workers ./workers
COPY migrations ./migrations

RUN useradd --uid 10001 --create-home service
USER service

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers"]
