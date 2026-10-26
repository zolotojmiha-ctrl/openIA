#!/usr/bin/env bash
# Run as root from the checked-out project. No secrets appear in arguments or logs.
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"
backup_root=${ARMADA_BACKUP_DIR:-/var/backups/armada}
mkdir -p -- "$backup_root"
chmod 700 -- "$backup_root"
exec 9>"$backup_root/.backup.lock"
flock -n 9 || { printf 'Another backup is already running.\n' >&2; exit 1; }
archive_stamp=$(date -u +%Y%m%dT%H%M%SZ)
staging=$(mktemp -d "$backup_root/.pending.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
compose=(docker compose -f compose.yml)
# Stop application writes, not PostgreSQL; pg_dump remains transactionally consistent.
"${compose[@]}" stop web mailworker
resume() { "${compose[@]}" start web mailworker >/dev/null; }
trap 'resume; rm -rf -- "$staging"' EXIT
"${compose[@]}" exec -T db pg_dump -U armada_admin -d armada --no-owner --no-acl -Fc > "$staging/database.dump"
"${compose[@]}" run --rm --no-deps --user 0:0 --entrypoint tar web -C /run/armada -cf - . > "$staging/config.tar"
"${compose[@]}" run --rm --no-deps --user 0:0 --entrypoint tar web -C /var/lib/armada-documents -cf - . > "$staging/documents.tar"
cp compose.yml Caddyfile "$staging/"
if [[ -f .env ]]; then cp .env "$staging/"; fi
"${compose[@]}" exec -T db pg_restore --list < "$staging/database.dump" > "$staging/database-contents.txt"
(cd "$staging"; sha256sum database.dump config.tar documents.tar > SHA256SUMS)
archive="$backup_root/armada-$archive_stamp.tar.gz"
tar -C "$staging" -czf "$archive.partial" .
mv -- "$archive.partial" "$archive"
resume
trap 'rm -rf -- "$staging"' EXIT
printf 'Backup saved: %s\n' "$archive"
