#!/usr/bin/env bash
# Host-side only. No Docker socket is exposed to the application.
set -euo pipefail
umask 077
cd -- "$(dirname -- "$0")"
backup_root=${ARMADA_BACKUP_DIR:-/var/backups/armada}
mkdir -p -- "$backup_root"
chmod 700 -- "$backup_root"
backup_root=$(cd -- "$backup_root" && pwd -P)
export ARMADA_BACKUP_DIR="$backup_root"
exec 8>"$backup_root/.disk-worker.lock"
flock -n 8 || exit 0
compose=(docker compose -f compose.yml)
job=$("${compose[@]}" exec -T web python server/manage.py disk_backup claim)
[[ -n "$job" ]] || exit 0
if [[ ! "$job" =~ ^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$ ]]; then
  printf 'Не удалось получить задание резервирования.\n' >&2
  exit 1
fi
work=$(mktemp -d "$backup_root/.disk-work.XXXXXX")
finished=0
cleanup() {
  if (( finished == 0 )); then
    "${compose[@]}" exec -T web python server/manage.py disk_backup fail --job "$job" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$work"
}
trap cleanup EXIT
# Reuse the tested, consistent backup including original config+documents+database.
if ! bash backup.sh > "$work/local-backup.log" 2>&1; then
  printf 'Локальная копия не создана. Проверьте журнал резервирования на VPS.\n' >&2
  exit 1
fi
archive=$(sed -n 's/^Backup saved: //p' "$work/local-backup.log" | tail -n 1)
if [[ ! -f "$archive" || "$(dirname -- "$archive")" != "$backup_root" || ! "$(basename -- "$archive")" =~ ^armada-[0-9]{8}T[0-9]{6}Z(-[0-9a-f]{8})?\.tar\.gz(\.encrypted)?$ ]]; then
  printf 'Не найден завершённый архив; загрузка остановлена.\n' >&2
  exit 1
fi
encrypted_args=()
if [[ "$archive" == *.encrypted ]]; then
  [[ -f "$archive.key-id" && ! -L "$archive.key-id" ]] || exit 1
  encrypted_args=(--encrypted --key-id-file /backup/key-id)
  key_mount=(--volume "$archive.key-id:/backup/key-id:ro")
else
  key_mount=()
fi
"${compose[@]}" run --rm --no-deps -T --user 0:0 "${key_mount[@]}"   --volume "$archive:/backup/source.tar.gz:ro"   --volume "$work:/backup/work"   --entrypoint python web server/manage.py disk_backup upload   --job "$job" --source /backup/source.tar.gz --work /backup/work "${encrypted_args[@]}"
finished=1
printf 'Резервная копия создана, зашифрована, загружена и проверена.\n'
