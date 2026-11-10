FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY deploy/synology/wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels --require-hashes -r /wheels/requirements.lock && rm -rf /wheels && useradd --uid 10001 --create-home armada
COPY --chown=10001:10001 server ./server
COPY --chown=10001:10001 portal ./portal
COPY --chown=10001:10001 index.html ./index.html
COPY --chown=10001:10001 deploy/synology/start.sh deploy/synology/init_secrets.py ./deploy/synology/
COPY --chown=10001:10001 server/static ./static
USER 10001:10001
ENV ARMADA_STATIC_ROOT=/app/static ARMADA_RUNTIME_DIR=/tmp/armada
EXPOSE 8000
CMD ["sh", "/app/deploy/synology/start.sh"]
