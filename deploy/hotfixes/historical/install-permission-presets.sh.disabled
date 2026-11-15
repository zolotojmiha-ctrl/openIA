#!/usr/bin/env bash
set -euo pipefail
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app_root="${ARMADA_APP_ROOT:-/opt/armada}"
export package_dir app_root
python3 - <<'PY'
import hashlib,json,os
from pathlib import Path
root=Path(os.environ['app_root']);package=Path(os.environ['package_dir'])
manifest=json.loads((package/'manifest.json').read_text())
if root.is_symlink() or not (root/'deploy/beget/compose.yml').is_file():raise SystemExit('Каталог приложения не прошёл проверку.')
for name,entry in manifest['files'].items():
    path=root/name
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):raise SystemExit('Небезопасный путь: '+name)
    current=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    if current not in [entry['before'],entry['after'],*entry.get('also_before',[])]:raise SystemExit('Исходный файл отличается. Ничего не заменено: '+name)
    if hashlib.sha256((package/'files'/name).read_bytes()).hexdigest()!=entry['after']:raise SystemExit('Повреждён файл исправления: '+name)
    if name.endswith('.py'):compile((package/'files'/name).read_bytes(),name,'exec')
# Keep the already installed money-form fix.
p=root/'server/projects/rental_views.py'
if hashlib.sha256(p.read_bytes()).hexdigest()!='ffff3c425b1b3190e12477d89d7bcc4a94039b4603903b0ec85399d3af249bc2':raise SystemExit('Сначала требуется ранее выданное исправление тарифа и аванса.')
for name,expected in manifest.get('requires',{}).items():
    if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:raise SystemExit('Сначала установите предыдущее исправление: '+name)
print('Файлы проверены. Предыдущие исправления сохранятся.')
PY
cd "$app_root"
max_was_running=0
if [ -n "$(docker compose --env-file deploy/beget/.env -f deploy/beget/compose.yml --profile max ps --status running -q maxworker)" ]; then max_was_running=1; fi
ARMADA_APP_ROOT="$app_root" bash deploy/beget/backup.sh
source_backup_root="${ARMADA_SOURCE_BACKUP_ROOT:-/var/backups/armada}"
install -d -m 700 "$source_backup_root"
source_backup="$(mktemp -d "$source_backup_root/permission-presets-source-XXXXXXXX")"
export source_backup
python3 - <<'PY'
import json,os,shutil
from pathlib import Path
root=Path(os.environ['app_root']);package=Path(os.environ['package_dir']);backup=Path(os.environ['source_backup'])
manifest=json.loads((package/'manifest.json').read_text())
for name in manifest['files']:
    destination=backup/name;destination.parent.mkdir(parents=True,exist_ok=True)
    if (root/name).exists():shutil.copy2(root/name,destination)
shutil.copy2(package/'manifest.json',backup/'manifest.json')
for name in manifest['files']:
    (root/name).parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(package/'files'/name,root/name)
    (root/name).chmod(0o644)
print('Копия исходников:',backup)
PY
docker compose --env-file deploy/beget/.env -f deploy/beget/compose.yml build web
docker compose --env-file deploy/beget/.env -f deploy/beget/compose.yml up -d --no-deps --force-recreate --wait --wait-timeout 180 web mailworker
if [ "$max_was_running" = 1 ]; then
  docker compose --env-file deploy/beget/.env -f deploy/beget/compose.yml --profile max up -d --no-deps --force-recreate --wait --wait-timeout 180 maxworker
fi
printf '%s\n' 'Наборы прав профессий и автоматическая подстановка обновлены. Обновите форму пользователя в браузере. Существующие права и привязки MAX сохранены; новые настройки применяются после сохранения выбранного сотрудника.'
