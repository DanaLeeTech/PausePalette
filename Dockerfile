# One image, two jobs: the static PWA (default CMD) and the CLI (compose entrypoint).

FROM python:3.12-slim
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY pausepalette ./pausepalette
RUN pip install .

# The site is built into the image once; nothing is written at runtime.
COPY tools/build_site.py ./tools/
COPY web ./web
RUN python tools/build_site.py dist

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "http.server", "8000", "--directory", "dist"]
