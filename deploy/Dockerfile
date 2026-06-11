FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir -e . && useradd -r gateway && mkdir -p /srv/archive && chown gateway /srv/archive
USER gateway
EXPOSE 8050
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8050/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8050"]
