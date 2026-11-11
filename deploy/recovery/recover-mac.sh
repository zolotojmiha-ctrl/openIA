#!/usr/bin/env bash
# Local Docker Desktop only. Never send the private key to VPS/Git/chat.
set -euo pipefail
umask 077
[[ $(uname -s) == Darwin ]] || { echo 'Эта команда предназначена для Mac.' >&2; exit 1; }
[[ $# == 1 && $1 =~ ^[a-f0-9]{64}$ ]] || { echo 'Укажите ожидаемый SHA-256 зашифрованной копии из ЛК.' >&2; exit 1; }
expected_hash=$1
project_dir=$(cd -- "$(dirname -- "$0")/../.." && pwd -P)
docker_binary=/Applications/Docker.app/Contents/Resources/bin/docker
[[ -x "$docker_binary" ]] || { echo 'Не найден Docker Desktop в Программах.' >&2; exit 1; }
# Select only the local Desktop engine, ignoring inherited remote selectors.
unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH
docker=("$docker_binary" --context desktop-linux)
endpoint=$("${docker[@]}" context inspect desktop-linux --format '{{.Endpoints.docker.Host}}')
[[ "$endpoint" == unix://* ]] || { echo 'Требуется локальный Docker Desktop, удалённый endpoint запрещён.' >&2; exit 1; }
os=$("${docker[@]}" info --format '{{.OperatingSystem}}')
[[ "$os" == *'Docker Desktop'* ]] || { echo 'Выбранный движок не подтверждён как Docker Desktop.' >&2; exit 1; }
archive=$(osascript -e 'POSIX path of (choose file with prompt "Выберите скачанную с Яндекс Диска резервную копию .encrypted")')
[[ -f "$archive" && "$archive" == *.encrypted && "$archive" != "$project_dir"/* && "$archive" != *,* ]] || { echo 'Выберите .encrypted вне каталога исходников; путь без запятых.' >&2; exit 1; }
actual_hash=$(shasum -a 256 "$archive" | cut -d ' ' -f 1)
[[ "$actual_hash" == "$expected_hash" ]] || { echo 'SHA-256 скачанной копии не совпал с ЛК. Проверка остановлена.' >&2; exit 1; }
private_key=$(osascript -e 'POSIX path of (choose file with prompt "Выберите скачанный в ЛК КЛЮЧ ВОССТАНОВЛЕНИЯ (не SSH-ключ и не ключ подписи)")')
[[ -f "$private_key" && "$private_key" != "$project_dir"/* && "$private_key" != *,* ]] || { echo 'Ключ должен быть отдельным файлом вне каталога исходников; путь без запятых.' >&2; exit 1; }
receipt_dir="$HOME/Documents/ArmadaRecoveryReceipts"
mkdir -p "$receipt_dir";chmod 700 "$receipt_dir"
receipt_name="restored-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"
image="armada-recovery-check:631"
postgres="postgres:17-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"
echo 'SHA-256 совпал. Подготавливаем инструменты; ключ и архив в сборку не передаются.'
"${docker[@]}" build --platform linux/amd64 --network none -t "$image" -f "$project_dir/deploy/recovery/Dockerfile" "$project_dir"
"${docker[@]}" pull --platform linux/amd64 "$postgres"
echo 'Проверяем расшифровку, файлы и восстановление PostgreSQL во временном окружении без сети.'
"${docker[@]}" run --rm --pull never --platform linux/amd64 --network none --read-only \
    --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true \
    --memory 512m --tmpfs /tmp:rw,size=2g,mode=1777 \
    --env DOCKER_DEFAULT_PLATFORM=linux/amd64 \
    --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock,readonly \
    --mount "type=bind,src=$archive,dst=/inputs/backup.encrypted,readonly" \
    --mount "type=bind,src=$private_key,dst=/inputs/recovery.pem,readonly" \
    --mount "type=bind,src=$receipt_dir,dst=/receipts" \
    "$image" --archive /inputs/backup.encrypted --private-key /inputs/recovery.pem \
    --receipt "/receipts/$receipt_name" --postgres-image "$postgres"
echo "Проверка завершена. Подтверждение сохранено в Documents/ArmadaRecoveryReceipts/$receipt_name"
echo 'Исходный архив и ключ не изменены; рабочий VPS не использовался.'
