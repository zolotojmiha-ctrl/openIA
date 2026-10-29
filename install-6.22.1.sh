#!/usr/bin/env bash
set -euo pipefail
umask 077
app_root=${ARMADA_APP_ROOT:-/opt/armada/openIA-feature-accounts-foundation}
if [[ ! -f "$app_root/deploy/beget/compose.yml" || ! -f "$app_root/deploy/beget/backup.sh" || $(basename -- "$app_root") != openIA-feature-accounts-foundation ]]; then
  printf 'Не найден существующий каталог приложения: %s\n' "$app_root" >&2
  exit 1
fi
if (( EUID != 0 )); then
  printf 'Запустите этот файл от root: sudo bash /tmp/armada-update-6.22.1.sh\n' >&2
  exit 1
fi
for command_name in curl sha256sum unzip docker flock; do
  command -v "$command_name" >/dev/null || { printf 'Не найдена команда: %s\n' "$command_name" >&2; exit 1; }
done
docker compose version >/dev/null
exec 9>"$app_root/.armada-update.lock"
flock -n 9 || { printf 'Другое обновление уже выполняется.\n' >&2; exit 1; }
staging=$(mktemp -d /tmp/armada-update-6.22.1.XXXXXX)
trap 'rm -rf -- "$staging"' EXIT
curl --fail --location --retry 3 --output "$staging/openIA-6.22.1-update.zip" 'https://github.com/zolotojmiha-ctrl/openIA/raw/refs/heads/downloads/nas-updates/openIA-6.22.1-update.zip'
(cd "$staging" && printf '%s  %s\n' 'ddbd4964b71d29e0df0260b3a16f7ea023d07ca6931644e2044cbba4b9df900b' 'openIA-6.22.1-update.zip' | sha256sum --check -)
unzip -tq "$staging/openIA-6.22.1-update.zip"
cd -- "$app_root"
printf 'Создаём резервную копию базы, документов и ключей.\n'
bash deploy/beget/backup.sh
unzip -oq "$staging/openIA-6.22.1-update.zip" -d "$(dirname -- "$app_root")"
docker compose -f deploy/beget/compose.yml build web
docker compose -f deploy/beget/compose.yml up -d --no-build
docker compose -f deploy/beget/compose.yml ps
printf 'Обновление 6.22.1 установлено. Обновите страницу личного кабинета.\n'
