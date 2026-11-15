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
# Guards derived ONLY from this script's own limits (not from an established cause of any past failure):
# check container 512m + PostgreSQL container 512m + 512 MiB headroom for the Docker VM;
# tmpfs /tmp is 2 GiB and must hold the decrypted copy plus its extracted files, so the
# encrypted copy is limited to a quarter of it. The checks are not loosened automatically.
min_vm_mib=1536; max_archive_mib=512
vm_bytes=$("${docker[@]}" info --format '{{.MemTotal}}')
[[ "$vm_bytes" =~ ^[0-9]+$ ]] || { echo 'Не удалось определить память Docker Desktop. Проверка не начата.' >&2; exit 1; }
echo "Среда: архитектура Mac $(uname -m); память виртуальной машины Docker $((vm_bytes / 1048576)) МиБ. Образы запускаются как linux/amd64 (на Apple Silicon - через эмуляцию)."
(( vm_bytes / 1048576 >= min_vm_mib )) || { echo "ОТКАЗ: у Docker Desktop меньше $min_vm_mib МиБ памяти; лимиты проверки в сумме требуют больше. Увеличьте память в настройках Docker Desktop. Проверка не ослаблялась и не начата." >&2; exit 1; }
archive=$(osascript -e 'POSIX path of (choose file with prompt "Выберите скачанную с Яндекс Диска резервную копию .encrypted")')
[[ -f "$archive" && "$archive" == *.encrypted && "$archive" != "$project_dir"/* && "$archive" != *,* ]] || { echo 'Выберите .encrypted вне каталога исходников; путь без запятых.' >&2; exit 1; }
archive_bytes=$(wc -c < "$archive" | tr -d ' ')
(( archive_bytes / 1048576 <= max_archive_mib )) || { echo "ОТКАЗ: размер копии $((archive_bytes / 1048576)) МиБ больше расчётного предела $max_archive_mib МиБ для временного хранилища проверки (/tmp 2 ГиБ). Лимиты не ослаблены; сообщите разработчику размер, остальное не раскрывая." >&2; exit 1; }
actual_hash=$(shasum -a 256 "$archive" | cut -d ' ' -f 1)
[[ "$actual_hash" == "$expected_hash" ]] || { echo 'SHA-256 скачанной копии не совпал с ЛК. Проверка остановлена.' >&2; exit 1; }
# Preserve the copy name in the receipt; a fixed mount alias would make every
# receipt refer to backup.encrypted instead of the copy actually verified.
archive_name=$(basename -- "$archive")
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
set +e
"${docker[@]}" run --rm --pull never --platform linux/amd64 --network none --read-only \
    --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true \
    --memory 512m --tmpfs /tmp:rw,size=2g,mode=1777 \
    --env DOCKER_DEFAULT_PLATFORM=linux/amd64 \
    --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock,readonly \
    --mount "type=bind,src=$archive,dst=/inputs/$archive_name,readonly" \
    --mount "type=bind,src=$private_key,dst=/inputs/recovery.pem,readonly" \
    --mount "type=bind,src=$receipt_dir,dst=/receipts" \
    "$image" --archive "/inputs/$archive_name" --private-key /inputs/recovery.pem \
    --receipt "/receipts/$receipt_name" --postgres-image "$postgres"
check_status=$?
set -e
if (( check_status != 0 )); then
  echo "Подтверждение НЕ получено. Код выхода контейнера проверки: $check_status." >&2
  if (( check_status == 137 )); then echo 'Код 137 означает принудительное завершение процесса (в том числе возможно по памяти). Это признак, причина не установлена.' >&2; fi
  echo 'Передайте разработчику только обезличенный вывод выше: этапы, коды и типы ошибок. Архив, ключ и содержимое данных не отправлять.' >&2
  exit "$check_status"
fi
echo "Проверка завершена. Подтверждение сохранено в Documents/ArmadaRecoveryReceipts/$receipt_name"
echo 'Исходный архив и ключ не изменены; рабочий VPS не использовался.'
