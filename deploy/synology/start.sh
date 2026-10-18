#!/bin/sh
set -eu
python server/manage.py migrate --noinput
exec gunicorn --chdir server config.wsgi:application --bind 0.0.0.0:8000 --workers 1 --threads 2 --worker-tmp-dir /tmp --timeout 60
