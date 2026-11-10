# One application layer keeps VFS builds small; dependency hashes remain mandatory.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN --mount=type=bind,source=.,target=/source,ro \
    pip install --no-cache-dir --no-index --find-links=/source/deploy/synology/wheels --require-hashes -r /source/deploy/synology/wheels/requirements.lock \
    && useradd --uid 10001 --create-home armada \
    && mkdir -p /app/deploy/synology \
    && cp -a /source/server /source/portal /source/index.html /app/ \
    && cp -a /source/deploy/synology/start.sh /source/deploy/synology/init_secrets.py /app/deploy/synology/ \
    && cp -a /source/server/static /app/static \
    && chown -R 10001:10001 /app
USER 10001:10001
ENV ARMADA_STATIC_ROOT=/app/static ARMADA_RUNTIME_DIR=/tmp/armada
EXPOSE 8000
CMD ["sh", "/app/deploy/synology/start.sh"]
