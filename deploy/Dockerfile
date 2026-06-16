FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir -e . && useradd -r gateway && mkdir -p /srv/archive && chown gateway /srv/archive
# Git commit SHA + build-time stamp baked into the image — read by GET /build-info;
# the sha is also exposed as an OCI revision label. Written as root (before
# USER gateway) so /srv ownership stays root; the files are world-readable.
# Defaults to "unknown" for local / non-deploy builds without --build-arg GIT_SHA.
ARG GIT_SHA=unknown
RUN date -u +%Y-%m-%dT%H:%M:%SZ > /srv/build_time.txt && \
    echo "${GIT_SHA}" > /srv/git_sha.txt
LABEL org.opencontainers.image.revision="${GIT_SHA}"
USER gateway
EXPOSE 8022
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8022/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8022"]
