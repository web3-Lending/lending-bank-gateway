FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
# 启动入口：先 alembic upgrade head 再 uvicorn（容器自迁移，与 lending-recon 一致）。
COPY deploy/entrypoint.sh /srv/entrypoint.sh
# PIP_DEFAULT_TIMEOUT/PIP_RETRIES 走 env，让 PEP517 build-isolation 子进程（拉 setuptools 等
# build 依赖）也继承——dev-hw→pythonhosted 偶发 read timeout，仅命令行 --timeout 覆盖不到子进程。
ENV PIP_DEFAULT_TIMEOUT=300 PIP_RETRIES=8
RUN pip install --no-cache-dir --timeout=300 --retries=8 -e . && useradd -r gateway \
    && mkdir -p /srv/archive && chown gateway /srv/archive && chmod +x /srv/entrypoint.sh
# Git commit SHA + build-time stamp baked into the image — read by GET /build-info;
# the sha is also exposed as an OCI revision label. Written as root (before
# USER gateway) so /srv ownership stays root; the files are world-readable.
# Defaults to "unknown" for local / non-deploy builds without --build-arg GIT_SHA.
ARG GIT_SHA=unknown
ARG APP_VERSION=0.1.0
ARG RELEASE_ID=not-reported
ARG SCHEMA_REVISION=schema_unknown
ARG REPO_URL=https://github.com/web3-Lending/lending-bank-gateway
RUN date -u +%Y-%m-%dT%H:%M:%SZ > /srv/build_time.txt && \
    echo "${GIT_SHA}" > /srv/git_sha.txt
LABEL org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="${REPO_URL}" \
      io.lending.project="lending-bank-gateway" \
      io.lending.service="lending-bank-gateway-api" \
      io.lending.release.id="${RELEASE_ID}" \
      io.lending.schema.revision="${SCHEMA_REVISION}"
USER gateway
EXPOSE 8022
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8022/healthz')"
CMD ["/srv/entrypoint.sh"]
