#!/usr/bin/env bash
# ARMADA 6.23.0. Existing Beget installation only; no data reset.
set -euo pipefail
umask 077
app_root=${ARMADA_APP_ROOT:-/opt/armada/openIA-feature-accounts-foundation}
if (( EUID != 0 )); then
  printf 'Запустите установщик от root.\n' >&2
  exit 1
fi
if [[ ! -f "$app_root/deploy/beget/compose.yml" || ! -f "$app_root/deploy/beget/backup.sh" || $(basename -- "$app_root") != openIA-feature-accounts-foundation ]]; then
  printf 'Не найден существующий каталог приложения: %s\n' "$app_root" >&2
  exit 1
fi
for command_name in curl sha256sum unzip docker flock df awk; do
  command -v "$command_name" >/dev/null || { printf 'Не найдена команда: %s\n' "$command_name" >&2; exit 1; }
done
docker compose version >/dev/null
docker info >/dev/null
exec 9>"$app_root/.armada-update.lock"
flock -n 9 || { printf 'Другое обновление уже выполняется.\n' >&2; exit 1; }
for target in "$app_root" /tmp; do
  available_kb=$(df -Pk -- "$target" | awk 'NR==2 {print $4}')
  if [[ ! "$available_kb" =~ ^[0-9]+$ ]] || (( available_kb < 2097152 )); then
    printf 'Недостаточно свободного места для безопасной сборки (нужно минимум 2 ГБ): %s\n' "$target" >&2
    exit 1
  fi
done
staging=$(mktemp -d /tmp/armada-update-6.23.0.XXXXXX)
trap 'rm -rf -- "$staging"' EXIT
trap 'printf "Обновление не завершено. Дальнейшие действия остановлены; не удаляйте тома или резервную копию.\n" >&2' ERR
curl --fail --location --retry 3 --output "$staging/openIA-6.23.0-update.zip" 'https://github.com/zolotojmiha-ctrl/openIA/raw/refs/heads/downloads/nas-updates/openIA-6.23.0-update.zip'
(cd "$staging" && printf '%s  %s\n' '9edd911e08baa6accfbd3b686910d9dc91dd253f8c00277bc72c729461721c1a' 'openIA-6.23.0-update.zip' | sha256sum --check -)
unzip -tq "$staging/openIA-6.23.0-update.zip"
unzip -oq "$staging/openIA-6.23.0-update.zip" -d "$staging/source"
package_root="$staging/source/openIA-feature-accounts-foundation"
printf 'Собираем новую версию до замены работающих файлов.\n'
docker build -f "$package_root/deploy/synology/Dockerfile" -t armada-web:6.23.0 "$package_root"
cd -- "$app_root"
printf 'Создаём резервную копию базы, документов, настроек и ключей.\n'
bash deploy/beget/backup.sh
unzip -oq "$staging/openIA-6.23.0-update.zip" -d "$(dirname -- "$app_root")"
compose=(docker compose -f deploy/beget/compose.yml)
"${compose[@]}" up -d --no-build
# Caddyfile is a bind-mounted file: recreate the proxy to load the new inode.
"${compose[@]}" up -d --no-build --no-deps --force-recreate proxy
healthy=0
for attempt in {1..90}; do
  web_id=$("${compose[@]}" ps -q web)
  worker_id=$("${compose[@]}" ps -q mailworker)
  proxy_id=$("${compose[@]}" ps -q proxy)
  if [[ -n "$web_id" && -n "$worker_id" && -n "$proxy_id" ]] &&
     [[ $(docker inspect --format '{{.State.Health.Status}}' "$web_id") == healthy ]] &&
     [[ $(docker inspect --format '{{.State.Running}}' "$worker_id") == true ]] &&
     [[ $(docker inspect --format '{{.State.Running}}' "$proxy_id") == true ]]; then
    healthy=1
    break
  fi
  sleep 2
done
if (( healthy == 0 )); then
  "${compose[@]}" ps
  printf 'Не подтверждён запуск приложения, почтового процесса или прокси. Нужна проверка журнала.\n' >&2
  exit 1
fi
"${compose[@]}" exec -T web python server/manage.py check
"${compose[@]}" exec -T web python server/manage.py site_forms_status
"${compose[@]}" ps
printf '\nОбновление 6.23.0 установлено. Обновите страницу сайта и кабинета.\n'
printf 'Анкеты и запросы: Почта → Заявки с сайта. Получатель писем: info@armada-sibir.ru.\n'
printf 'Если проверка выше просит подключить info, подключите его в разделе Почта.\n'
printf 'Проверьте одну отправку с сайта и получение письма в info; установщик письма не отправляет.\n'
