# Offline recovery tool; bundled wheels target linux/amd64.
FROM docker:29-cli@sha256:2357639ad1ea7d0194134bf3292622fd4e562483966ed674e5d7c945b5f3b28d AS docker_cli
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /opt/armada
# One snapshot avoids multiplying VFS disk usage in verification environments.
RUN --mount=type=bind,source=.,target=/source,ro \
    --mount=from=docker_cli,source=/usr/local/bin/docker,target=/tmp/docker-cli,ro \
    pip install --no-index --find-links=/source/deploy/synology/wheels --require-hashes -r /source/deploy/synology/wheels/requirements.lock \
    && cp /tmp/docker-cli /usr/local/bin/docker \
    && chmod 0755 /usr/local/bin/docker \
    && mkdir -p deploy/beget server/projects \
    && cp /source/deploy/beget/verify_backup.py /source/deploy/beget/backup_manifest.py /source/deploy/beget/backup_counts.sql deploy/beget/ \
    && cp /source/server/projects/disk_crypto.py server/projects/ \
    && cp /source/deploy/recovery/check.py deploy/beget/check_recovery.py
ENTRYPOINT ["python", "deploy/beget/check_recovery.py"]
