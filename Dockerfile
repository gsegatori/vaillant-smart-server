FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN pip install --prefix=/install .


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 vaillant

COPY --from=builder /install /usr/local
COPY app /opt/app/app

ENV PYTHONPATH=/opt/app
WORKDIR /opt/app

RUN mkdir -p /data && chown vaillant:vaillant /data
USER vaillant
VOLUME ["/data"]

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS "http://localhost:${BIND_PORT:-5000}/healthz" > /dev/null || exit 1

CMD ["python", "-m", "app.main"]
