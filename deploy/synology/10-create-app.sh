#!/bin/sh
set -eu
# The official image sources non-executable .sh init files as postgres.
APP_PASSWORD="$(cat /run/armada/postgres-app-password)"
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 --set=app_password="$APP_PASSWORD" <<'SQL'
CREATE ROLE armada_app LOGIN PASSWORD :'app_password';
ALTER DATABASE armada OWNER TO armada_app;
GRANT USAGE, CREATE ON SCHEMA public TO armada_app;
SQL
unset APP_PASSWORD
