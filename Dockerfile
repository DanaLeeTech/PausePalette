# syntax=docker/dockerfile:1

# ---- build: install dependencies into a venv --------------------------------
FROM python:3.12-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY pausepalette ./pausepalette
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install ".[web]"

# ---- runtime ----------------------------------------------------------------
FROM python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PAUSEPALETTE_DATA=/data/jobs
WORKDIR /app

COPY --from=build /opt/venv /opt/venv
COPY pausepalette ./pausepalette
COPY webapp ./webapp
COPY docker/entrypoint.sh /usr/local/bin/pausepalette-entrypoint

RUN chmod +x /usr/local/bin/pausepalette-entrypoint \
 && useradd --uid 10001 --create-home app \
 && mkdir -p /data/jobs \
 && chown -R app:app /data

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["pausepalette-entrypoint"]
CMD ["web"]
