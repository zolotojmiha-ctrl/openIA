#!/usr/bin/env bash
# Run as root from the checked-out project. No secrets appear in arguments or logs.
set -euo pipefail
umask 077
tool_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
hold_writers=0
compose_file="$(dirname -- "$0")/compose.yml"
state_file=
lock_fd=
while (( $# )); do
  case "$1" in
    --hold-writers) hold_writers=1; shift;;
    --compose-file) compose_file=$2; shift 2;;
    --state-file) state_file=$2; shift 2;;
    --lock-fd) lock_fd=$2; shift 2;;
    *) printf 'Unknown backup option.\n' >&2; exit 2;;
  esac
done
backup_python=${ARMADA_BACKUP_PYTHON:-python3}
if [[ -n "${ARMADA_BACKUP_PUBLIC_KEY:-}" ]]; then
  [[ -f "$ARMADA_BACKUP_PUBLIC_KEY" && ! -L "$ARMADA_BACKUP_PUBLIC_KEY" ]] || exit 2
  "$backup_python" -c 'import cryptography' >/dev/null
fi
[[ ${ARMADA_BACKUP_KEEP:-7} =~ ^[1-9][0-9]*$ && ${ARMADA_BACKUP_DAYS:-30} =~ ^[1-9][0-9]*$ ]] || exit 2
compose_file=$(realpath -- "$compose_file")
cd -- "$(dirname -- "$compose_file")"
if (( hold_writers )) && [[ -z "$state_file" ]]; then printf 'Held backup requires writer state file.\n' >&2; exit 2; fi
backup_root=${ARMADA_BACKUP_DIR:-/var/backups/armada}
[[ "$backup_root" == /* ]] || { printf 'Backup directory must be absolute.\n' >&2; exit 2; }
mkdir -p -- "$backup_root"
chmod 700 -- "$backup_root"
if [[ -n "$lock_fd" ]]; then
  [[ "$lock_fd" =~ ^[0-9]+$ ]] || exit 2
  python3 - "$lock_fd" "$backup_root/.backup.lock" <<'PY'
import os,sys
fd=os.fstat(int(sys.argv[1]));path=os.stat(sys.argv[2]);assert (fd.st_dev,fd.st_ino)==(path.st_dev,path.st_ino),'Wrong backup lock descriptor'
PY
  flock -n "$lock_fd" || exit 1
else
  exec 9>"$backup_root/.backup.lock"
  flock -n 9 || { printf 'Another backup is already running.\n' >&2; exit 1; }
fi
archive_stamp=$(date -u +%Y%m%dT%H%M%SZ)
staging=$(mktemp -d "$backup_root/.pending.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
compose=(docker compose -f "$compose_file")
# Stop application writes, not PostgreSQL; pg_dump remains transactionally consistent.
# Freeze every running writer, including the opt-in MAX worker. Resume only
# services which actually ran before backup; never enable MAX as a side effect.
running_writers=$("${compose[@]}" --profile max ps --services --status running web mailworker maxworker)
writers=()
while IFS= read -r service; do
  case "$service" in web|mailworker|maxworker) writers+=("$service");; '') ;; *) printf 'Unexpected writer service.\n' >&2; exit 1;; esac
done <<< "$running_writers"
if [[ -n "$state_file" ]]; then
  (set -o noclobber; printf '%s\n' "${writers[@]}" > "$state_file")
fi
resume() { if (( hold_writers == 0 && ${#writers[@]} )); then "${compose[@]}" --profile max start "${writers[@]}" >/dev/null; fi; }
trap 'resume; rm -rf -- "$staging"' EXIT
if (( ${#writers[@]} )); then "${compose[@]}" --profile max stop "${writers[@]}"; fi
"${compose[@]}" exec -T db pg_dump -U armada_admin -d armada --no-owner --no-acl -Fc > "$staging/database.dump"
"${compose[@]}" run --rm --no-deps --cap-add DAC_OVERRIDE --user 0:0 --entrypoint tar web -C /run/armada -cf - . > "$staging/config.tar"
"${compose[@]}" run --rm --no-deps --cap-add DAC_OVERRIDE --user 0:0 --entrypoint tar web -C /var/lib/armada-documents -cf - . > "$staging/documents.tar"
"${compose[@]}" exec -T db psql -U armada_admin -d armada -At -c "$(cat "$tool_dir/backup_counts.sql")" > "$staging/row-counts.json"
python3 "$tool_dir/backup_manifest.py" --directory "$staging"
cp compose.yml Caddyfile "$staging/"
if [[ -f .env ]]; then cp .env "$staging/"; fi
"${compose[@]}" exec -T db pg_restore --list < "$staging/database.dump" > "$staging/database-contents.txt"
(cd "$staging"; sha256sum database.dump config.tar documents.tar row-counts.json content-sha256.json > SHA256SUMS)
archive="$backup_root/armada-$archive_stamp-$(python3 -c 'import secrets; print(secrets.token_hex(4))').tar.gz"
tar -C "$staging" -czf "$archive.partial" .
if [[ -n "${ARMADA_BACKUP_PUBLIC_KEY:-}" ]]; then
  "$backup_python" "$tool_dir/../../server/projects/disk_crypto.py" --source "$archive.partial" --public-key "$ARMADA_BACKUP_PUBLIC_KEY" --output "$archive.encrypted"
  rm -- "$archive.partial"
  archive="$archive.encrypted"
else
  mv -- "$archive.partial" "$archive"
fi
resume
trap 'rm -rf -- "$staging"' EXIT
python3 "$tool_dir/backup_retention.py" --directory "$backup_root" --keep "${ARMADA_BACKUP_KEEP:-7}" --days "${ARMADA_BACKUP_DAYS:-30}" --apply || printf 'Очистка копий не завершена; новая копия сохранена.\n' >&2
printf 'Backup saved: %s\n' "$archive"
