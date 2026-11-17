#!/usr/bin/env bash
# ARMADA 6.33.3. Existing installation only. No data reset or implicit MAX activation.
set -euo pipefail
umask 077
# Imports of package helpers must not leave bytecode that would be copied into the new tree.
export PYTHONDONTWRITEBYTECODE=1
app_root=${ARMADA_APP_ROOT:-/opt/armada/openIA-feature-accounts-foundation}
allow_local_files=0
allow_unencrypted_backup=0
print_trust_fingerprint=0
for argument in "$@"; do
  case "$argument" in
    --allow-local-files) allow_local_files=1;;
    --allow-unencrypted-backup) allow_unencrypted_backup=1;;
    --print-trust-fingerprint) print_trust_fingerprint=1;;
    *) printf 'Неизвестный параметр: %s\nДопустимы: --allow-local-files, --allow-unencrypted-backup, --print-trust-fingerprint\n' "$argument" >&2; exit 2;;
  esac
done
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
script_path="$script_dir/$(basename -- "${BASH_SOURCE[0]}")"
# Trust anchor belongs to the operator, not to the downloaded archive.
trust_key=${ARMADA_RELEASE_PUBLIC_KEY:-/etc/armada/release-signing-public.pem}
check_trust_key() {
  [[ -f "$trust_key" && ! -L "$trust_key" ]] || { printf 'Нет независимого доверенного ключа подписи. Установка запрещена.\n' >&2; exit 1; }
  python3 - "$trust_key" <<'PY'
import os,stat,sys
s=os.stat(sys.argv[1]);assert s.st_uid==0 and not s.st_mode & 0o022,'Trust key must be root-owned and not writable by others'
PY
}
if (( print_trust_fingerprint )); then
  # Read-only: no Docker, no archive, no network. Compare the result with the
  # fingerprint you obtained from the release owner through an independent channel.
  for command_name in openssl sha256sum python3; do command -v "$command_name" >/dev/null || { printf 'Не найдена команда: %s\n' "$command_name" >&2; exit 1; }; done
  check_trust_key
  fingerprint=$(openssl pkey -pubin -in "$trust_key" -outform DER | sha256sum | awk '{print $1}')
  [[ "$fingerprint" =~ ^[0-9a-f]{64}$ ]] || { printf 'Не удалось вычислить отпечаток: файл не является открытым ключом.\n' >&2; exit 1; }
  printf 'Формат: SHA-256 от DER-кодировки SubjectPublicKeyInfo открытого ключа, 64 символа в нижнем регистре.\n'
  printf 'Отпечаток доверенного ключа: %s\n' "$fingerprint"
  printf 'Независимая проверка без этого скрипта: openssl pkey -pubin -in <ключ.pem> -outform DER | sha256sum\n'
  exit 0
fi
if (( EUID != 0 )); then printf 'Запустите установщик от root.\n' >&2; exit 1; fi
for command_name in curl sha256sum unzip docker flock df awk python3 openssl; do
  command -v "$command_name" >/dev/null || { printf 'Не найдена команда: %s\n' "$command_name" >&2; exit 1; }
done
python3 -c 'import sys; assert sys.version_info >= (3,9)' || { printf 'Требуется Python 3.9 или новее.\n' >&2; exit 1; }
if [[ -L "$app_root" || ! -f "$app_root/deploy/beget/compose.yml" || ! -f "$app_root/deploy/beget/backup.sh" || ( $(basename -- "$app_root") != openIA-feature-accounts-foundation && $(basename -- "$app_root") != armada ) ]]; then
  printf 'Не найден штатный каталог существующей установки: %s\n' "$app_root" >&2; exit 1
fi
docker compose version >/dev/null
docker info >/dev/null
# Backups without ARMADA_BACKUP_PUBLIC_KEY are plaintext; the operator must say so explicitly.
if [[ -z "${ARMADA_BACKUP_PUBLIC_KEY:-}" ]]; then
  if (( allow_unencrypted_backup == 0 )); then
    printf 'ОТКАЗ: ARMADA_BACKUP_PUBLIC_KEY не задан, предустановочная копия была бы НЕЗАШИФРОВАННОЙ.\nЗадайте ключ шифрования копий или явно разрешите незашифрованную копию параметром --allow-unencrypted-backup.\nСлужбы не останавливались, файлы не менялись.\n' >&2
    exit 1
  fi
  printf 'ВНИМАНИЕ: продолжаем с --allow-unencrypted-backup. Копия будет создана БЕЗ шифрования и будет содержать базу, документы и закрытые настройки.\n' >&2
fi
exec 9>"$app_root/.armada-update.lock"
flock -n 9 || { printf 'Другое обновление уже выполняется.\n' >&2; exit 1; }
cd -- "$app_root"
compose=(docker compose)
# Match the operator's established deploy/beget/.env commands explicitly.
# The tree switch preserves this exact file; no MAX or provider setting is reset.
if [[ -f "$app_root/deploy/beget/.env" ]]; then compose+=(--env-file "$app_root/deploy/beget/.env"); fi
compose+=(-f "$app_root/deploy/beget/compose.yml")
# Read the actual running image, without importing settings or displaying secrets.
version_code='import ast,pathlib; tree=ast.parse(pathlib.Path("/app/server/accounts/context.py").read_text()); versions=[v.value for n in ast.walk(tree) if isinstance(n,ast.Dict) for k,v in zip(n.keys,n.values) if isinstance(k,ast.Constant) and k.value=="release_version" and isinstance(v,ast.Constant)]; assert len(versions)==1; print(versions[0])'
installed=$("${compose[@]}" exec -T web python -c "$version_code")
case "$installed" in
  6.25.0|6.25.1|6.26.0|6.27.0|6.28.0|6.29.0|6.30.0|6.31.0|6.32.0|6.33.0|6.33.1|6.33.2|6.33.3) ;;
  *) printf 'Версия работающего приложения не поддержана или не определена. Сначала проверьте восстановленную установку.\n' >&2; exit 1;;
esac
staging=$(mktemp -d /tmp/armada-update-6.33.3.XXXXXX)
# EXIT also handles explicit failures, not only commands triggering ERR.
recovery=
phase=prepare
original_web_id=
original_web_tag=
switched=0
completed=0
# The phase variable changes only AFTER the phase file is durably written
# (fsync of file and directory). Unconfirmed progress is never treated as success.
set_phase() {
  python3 - "$recovery" "$1" <<'PY'
import datetime,os,sys
r,name=sys.argv[1:3]
fd=os.open(r+'/phase.tmp',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600);os.write(fd,(name+'\n').encode());os.fsync(fd);os.close(fd)
os.replace(r+'/phase.tmp',r+'/phase')
omsk=datetime.timezone(datetime.timedelta(hours=6))
fd=os.open(r+'/phases.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600);os.write(fd,(datetime.datetime.now(omsk).isoformat(timespec='seconds')+' (Asia/Omsk) '+name+'\n').encode());os.fsync(fd);os.close(fd)
d=os.open(r,os.O_RDONLY);os.fsync(d);os.close(d)
PY
  phase=$1
}
finish() {
  status=$?
  trap - EXIT INT TERM
  if (( completed == 0 )) && [[ -n "$recovery" ]]; then
    set +e
    if [[ "$phase" == migrate_started || "$phase" == migrate_done || "$phase" == verify ]]; then
      if ! "${compose[@]}" --profile max stop web mailworker maxworker; then
        printf 'Не удалось подтвердить остановку: проверьте Docker и вручную остановите писателей.\n' >&2
      fi
      # Diagnostics without secrets: phase, exit status, time. No command lines, no environment.
      printf 'phase=%s\nexit_status=%s\nwriters=stopped_by_installer_not_resumed\n' "$phase" "$status" > "$recovery/failure.txt" 2>/dev/null
      if [[ "$phase" == migrate_started ]]; then
        printf '\n## Состояние при сбое: migrate_started\nЗавершение миграций НЕ подтверждено. Путь А запрещён. Основной путь - Б (согласованное восстановление).\n' >> "$recovery/RECOVERY.md"
      else
        printf '\n## Состояние при сбое: %s\nМиграции подтверждённо завершены (migrate_done записан). Допустимы оба пути: А (диагностика и исправление новой версии) и Б (восстановление).\n' "$phase" >> "$recovery/RECOVERY.md"
      fi
      printf 'Установка остановлена после начала миграций (фаза %s). Подана команда остановки всех писателей, автозапуска не будет.\nКаталог recovery СОДЕРЖИТ закрытые настройки (.env): не публиковать, не копировать в облако. Инструкция: %s/RECOVERY.md\n' "$phase" "$recovery" >&2
    elif [[ -f "$recovery/writers" ]]; then
      if (( switched )); then
        "${compose[@]}" --profile max stop web mailworker maxworker
        python3 "$package_root/deploy/beget/switch_tree.py" rollback --app "$app_root" --recovery "$recovery"
        cd -- "$app_root"
      fi
      if [[ "$original_web_tag" == armada-web:6.33.3 && -n "$original_web_id" ]]; then
        docker image tag "$original_web_id" "$original_web_tag"
      fi
      original=()
      while IFS= read -r service; do [[ -z "$service" ]] || original+=("$service"); done < "$recovery/writers"
      if (( ${#original[@]} )); then
        rollback_compose=("${compose[@]}" -f "$recovery/images.json" --profile max)
        "${rollback_compose[@]}" up -d --no-build --no-deps --force-recreate web
        restore_status=$?
        old_healthy=0
        if (( restore_status == 0 )); then
          for attempt in {1..30}; do
            cid=$("${rollback_compose[@]}" ps -q web)
            if [[ -n "$cid" && $(docker inspect --format '{{.State.Health.Status}}' "$cid") == healthy ]]; then old_healthy=1; break; fi
            sleep 2
          done
        fi
        if (( old_healthy )); then
          "${rollback_compose[@]}" exec -T web python server/manage.py check &&
          "${rollback_compose[@]}" exec -T web python server/manage.py migrate --check
          restore_status=$?
        else restore_status=1; fi
        if (( restore_status == 0 )); then
          resumed=()
          for service in "${original[@]}"; do [[ "$service" == web ]] || resumed+=("$service"); done
          if (( ${#resumed[@]} )); then
            "${rollback_compose[@]}" up -d --no-build --no-deps --force-recreate "${resumed[@]}"
            restore_status=$?
          fi
        fi
        if (( restore_status == 0 )); then printf 'До миграций: прежнее приложение проверено, службы возобновлены. Каталог recovery (содержит закрытые настройки, не публиковать): %s\n' "$recovery" >&2
        else
          "${rollback_compose[@]}" stop "${original[@]}"
          printf 'Автовозврат не подтверждён; писатели остановлены. См. %s/RECOVERY.md\n' "$recovery" >&2
        fi
      fi
    fi
  fi
  rm -rf -- "$staging"
  if (( completed == 0 && status == 0 )); then status=1; fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
archive_path=${ARMADA_UPDATE_ARCHIVE:-}
if [[ -z "$archive_path" && -f "$script_dir/openIA-6.33.3-update.zip" ]]; then
  archive_path="$script_dir/openIA-6.33.3-update.zip"
fi
if [[ -n "$archive_path" ]]; then
  [[ -f "$archive_path" ]] || { printf 'Архив не найден.\n' >&2; exit 1; }
  cp -- "$archive_path" "$staging/update.zip"
else
  curl --fail --location --retry 3 --output "$staging/update.zip" 'https://github.com/zolotojmiha-ctrl/openIA/raw/refs/heads/downloads/nas-updates/openIA-6.33.3-update.zip'
fi
check_trust_key
for artifact in SHA256SUMS-6.33.3 SHA256SUMS-6.33.3.sig openIA-6.33.3-update.json; do
  if [[ -f "$script_dir/$artifact" ]]; then
    cp -- "$script_dir/$artifact" "$staging/$artifact"
  else
    curl --fail --location --retry 3 --output "$staging/$artifact" "https://github.com/zolotojmiha-ctrl/openIA/raw/refs/heads/downloads/nas-updates/$artifact"
  fi
done
openssl dgst -sha256 -verify "$trust_key" -signature "$staging/SHA256SUMS-6.33.3.sig" "$staging/SHA256SUMS-6.33.3" >/dev/null 2>&1 || { printf 'Подпись выпуска не подтверждена. Установка запрещена.\n' >&2; exit 1; }
python3 - "$staging" "$script_path" <<'PY'
import hashlib,json,pathlib,re,sys
root=pathlib.Path(sys.argv[1]);rows={}
for line in (root/'SHA256SUMS-6.33.3').read_text().splitlines():
    m=re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9_.-]+)',line)
    assert m and m[2] not in rows,'Invalid signed manifest';rows[m[2]]=m[1]
files={'openIA-6.33.3-update.zip':root/'update.zip','install-6.33.3.sh':pathlib.Path(sys.argv[2]),'openIA-6.33.3-update.json':root/'openIA-6.33.3-update.json'}
assert set(rows)==set(files),'Unexpected signed manifest contents'
for name,path in files.items():assert hashlib.sha256(path.read_bytes()).hexdigest()==rows[name],'Signed artifact mismatch'
assert rows['openIA-6.33.3-update.zip']=='6d9f16b560cdfc3fc9cf960556568464d822e6395108764bf8d3673d97102f29','Archive differs from installer'
meta=json.loads(files['openIA-6.33.3-update.json'].read_text())
assert meta.get('version')=='6.33.3' and meta.get('ready_for_install') is True,'Release is not validated'
assert re.fullmatch('[0-9a-f]{40}',meta.get('source_commit','')),'Source commit missing'
PY
unzip -tq "$staging/update.zip"
python3 - "$staging/update.zip" <<'PY'
import ast,stat,sys,zipfile
from pathlib import PurePosixPath
prefix='openIA-feature-accounts-foundation/'
with zipfile.ZipFile(sys.argv[1]) as z:
    names=z.namelist()
    assert names and len(set(names))==len(names),'Duplicate or empty archive'
    for item in z.infolist():
        name=item.filename; parts=PurePosixPath(name).parts
        assert name.startswith(prefix) and '\\' not in name and '..' not in parts,'Unsafe archive path'
        assert not stat.S_ISLNK(item.external_attr>>16),'Archive symlink'
        assert not any(p in ('.git','.runtime','__pycache__','.env') for p in parts),'Private runtime file'
    required=['index.html','server/config/settings.py','server/accounts/context.py','deploy/synology/Dockerfile','deploy/beget/compose.yml','deploy/beget/backup.sh','deploy/beget/switch_tree.py']
    assert all(prefix+n in names for n in required),'Missing package file'
    tree=ast.parse(z.read(prefix+'server/accounts/context.py').decode())
    versions=[v.value for n in ast.walk(tree) if isinstance(n,ast.Dict) for k,v in zip(n.keys,n.values) if isinstance(k,ast.Constant) and k.value=='release_version' and isinstance(v,ast.Constant)]
    assert versions==['6.33.3'],'Wrong package version'
PY
# Only read-only service actions are allowed before the writers are held:
# docker info/inspect, compose ps/config, one SELECT pg_database_size, one
# throw-away read-only "du" container, df, and reading the archive. Nothing
# below until "Создаём копию" stops a service or changes the working tree.
original_web_container=$("${compose[@]}" ps -q web)
original_web_id=$(docker inspect --format '{{.Image}}' "$original_web_container")
original_web_tag=$(docker inspect --format '{{.Config.Image}}' "$original_web_container")
backup_root=${ARMADA_BACKUP_DIR:-/var/backups/armada}
[[ "$backup_root" == /* ]] || { printf 'Каталог копий должен быть абсолютным.\n' >&2; exit 2; }
printf 'Оцениваем пиковый расход места (до остановки служб и изменения дерева).\n'
db_bytes=$("${compose[@]}" exec -T db psql -U armada_admin -d armada -At -c 'SELECT pg_database_size(current_database())' 2>/dev/null) || db_bytes=
files_kb=$("${compose[@]}" run --rm --no-deps -T --cap-add DAC_OVERRIDE --user 0:0 --entrypoint du web -sk /run/armada /var/lib/armada-documents 2>/dev/null) || files_kb=
image_bytes=$(docker image inspect --format '{{.Size}}' "$original_web_id" 2>/dev/null) || image_bytes=
docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null) || docker_root=
python3 - "$staging/update.zip" "$staging" "$backup_root" "$app_root" "$docker_root" "$db_bytes" "$files_kb" "$image_bytes" "$([[ -n "${ARMADA_BACKUP_PUBLIC_KEY:-}" ]] && echo 1 || echo 0)" <<'PY'
import math,os,subprocess,sys,zipfile
zip_path,staging,backup_root,app_root,docker_root,db_bytes,files_kb,image_bytes,encrypted=sys.argv[1:10]
MIB=1048576;MARGIN_NUM,MARGIN_DEN=5,4;RESERVE=512*MIB
def fail(text):
    print(text+' Установка не начата: службы не останавливались, рабочее дерево не менялось.',file=sys.stderr);sys.exit(1)
def number(value,label):
    value=value.strip()
    if not value.isdigit():fail('Не удалось измерить: '+label+'.')
    return int(value)
rows=[line.split('\t')[0].strip() for line in files_kb.splitlines() if line.strip()]
if len(rows)!=2:fail('Не удалось измерить размер конфигурации и документов.')
files_bytes=sum(number(x,'размер конфигурации и документов') for x in rows)*1024
db=number(db_bytes,'размер базы данных');image=number(image_bytes,'размер текущего образа')
with zipfile.ZipFile(zip_path) as z:unpacked=sum(i.file_size for i in z.infolist())
archive=os.path.getsize(zip_path)
if not docker_root or not os.path.isdir(docker_root):fail('Каталог данных Docker недоступен на этом сервере, расход места на образы измерить нельзя.')
def existing(path):
    path=os.path.abspath(path)
    while not os.path.isdir(path):
        parent=os.path.dirname(path)
        if parent==path:fail('Не найден существующий родительский каталог для проверки места.')
        path=parent
    return path
parent=existing(os.path.dirname(app_root))
if os.stat(app_root).st_dev!=os.stat(parent).st_dev:fail('Каталог приложения и каталог recovery находятся на разных файловых системах; переключение дерева невозможно.')
data=db+files_bytes
# Upper bounds, not exact sizes. See docs/INSTALLER_SPACE_ESTIMATE in RELEASE_6_33_1.md.
# staging: update.zip copy + unpacked tree. backup: staging dump/config/documents,
# the .partial tar.gz and, with a key, the .encrypted copy exist at the same time
# (compression is not credited). recovery/app: copy of the new tree (rename of the old
# tree on one filesystem is not a second copy). Docker: new image layers, at most the
# current image size plus the unpacked source.
needs=[('временный каталог установки',existing(os.path.dirname(staging)),archive+unpacked),
       ('каталог резервных копий',existing(backup_root),(3 if encrypted=='1' else 2)*data),
       ('каталог приложения и recovery',parent,unpacked),
       ('хранилище Docker',existing(docker_root),image+unpacked)]
groups={}
for role,path,size in needs:
    result=subprocess.run(['df','-Pk','--',path],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    lines=result.stdout.splitlines()
    if result.returncode or len(lines)<2:fail('Не удалось измерить свободное место: '+role+'.')
    fields=lines[1].split()
    if len(fields)<6 or not fields[3].isdigit():fail('Не удалось измерить свободное место: '+role+'.')
    # Bind mounts of the same device have different targets but share capacity.
    # Repeated generic sources (e.g. overlay) are grouped conservatively too:
    # overestimation is preferable to counting one pool of free bytes twice.
    key=fields[0]
    mount=' '.join(fields[5:])
    entry=groups.setdefault(key,{'avail':int(fields[3])*1024,'need':0,'roles':[],'mounts':[]})
    entry['avail']=min(entry['avail'],int(fields[3])*1024)
    if mount not in entry['mounts']:entry['mounts'].append(mount)
    entry['need']+=size;entry['roles'].append(role)
short=[]
for device,entry in groups.items():
    required=math.ceil(entry['need']*MARGIN_NUM/MARGIN_DEN)+RESERVE
    line='  том %s: нужно не менее %d МиБ, доступно %d МиБ (%s)'%(', '.join(entry['mounts']),math.ceil(required/MIB),entry['avail']//MIB,'; '.join(entry['roles']))
    print(line)
    if entry['avail']<required:short.append(line)
if short:
    print('Недостаточно места (оценка пикового расхода с запасом 25% и 512 МиБ на файловую систему; это не гарантия точного размера):',file=sys.stderr)
    for line in short:print(line,file=sys.stderr)
    fail('Освободите место или перенесите каталог копий на другой том.')
PY
unzip -oq "$staging/update.zip" -d "$staging/source"
package_root="$staging/source/openIA-feature-accounts-foundation"
# Backup configuration is checked with the package's own backup.sh before any service is touched.
bash "$package_root/deploy/beget/backup.sh" --preflight
# Compare the existing tree with the verified release; show relative paths only.
python3 - "$package_root" "$app_root" "$staging/local-files.txt" <<'PY'
import os,re,sys
from pathlib import Path
package,app,out=map(Path,sys.argv[1:4])
def tree(root):
    found=set()
    for base,dirs,files in os.walk(root,followlinks=False):
        rel=Path(base).relative_to(root)
        if rel==Path('.') and '.git' in dirs:dirs.remove('.git')
        for d in list(dirs):
            if (Path(base)/d).is_symlink():found.add((rel/d).as_posix());dirs.remove(d)
        for f in files:found.add((rel/f).as_posix())
    return found
def listed(path):
    if path.is_symlink() or not path.is_file():return set()
    return {x for x in path.read_text().splitlines() if x and not x.startswith('/') and '..' not in x.split('/')}
known=tree(package)|listed(package/'deploy/beget/known-release-paths.txt')|listed(app/'deploy/beget/release-files.txt')
dotenv=re.compile(r'\.env(\..+)?')
extra=[];blocked=[]
for path in sorted(tree(app)):
    parts=path.split('/')
    if path=='.armada-update.lock':continue
    if len(parts) in (1,3) and (len(parts)==1 or parts[:2]==['deploy','beget']) and dotenv.fullmatch(parts[-1]):
        if (app/path).is_symlink() or (app/path).is_dir():blocked.append(path)
        continue
    if path not in known:extra.append(path)
if blocked:
    print('ОТКАЗ: служебный файл настроек является ссылкой или каталогом, нужен ручной просмотр:',*blocked,sep='\n  ',file=sys.stderr);sys.exit(1)
out.write_text(''.join(x+'\n' for x in extra))
PY
if [[ -s "$staging/local-files.txt" ]]; then
  printf 'В каталоге приложения есть файлы, которых нет в проверенном выпуске (только относительные пути, содержимое не читалось):\n' >&2
  head -n 100 "$staging/local-files.txt" | sed 's/^/  /' >&2
  if (( $(wc -l < "$staging/local-files.txt") > 100 )); then printf '  ... и ещё %s\n' "$(( $(wc -l < "$staging/local-files.txt") - 100 ))" >&2; fi
  if (( allow_local_files == 0 )); then
    printf 'ОТКАЗ до остановки служб. Если файлы нужно сохранить, запустите с --allow-local-files: они останутся в recovery/tree, но в новую версию автоматически НЕ переносятся (override-файлы не применяются).\n' >&2
    exit 1
  fi
  printf 'Продолжаем с --allow-local-files: файлы сохранятся только в recovery/tree, перечень будет в recovery/local-files.txt. В новой версии они не применяются автоматически.\n' >&2
fi
recovery_parent=$(dirname -- "$app_root")
python3 - "$recovery_parent" <<'PY'
import os,stat,sys
st=os.lstat(sys.argv[1])
assert stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode),'Родительский каталог recovery не является обычным каталогом'
assert st.st_uid==0,'Родительский каталог recovery должен принадлежать root'
assert not (st.st_mode & 0o022) or st.st_mode & stat.S_ISVTX,'Родительский каталог recovery доступен на запись посторонним'
PY
printf 'Собираем 6.33.3 до остановки работающего приложения.\n'
docker build -f "$package_root/deploy/synology/Dockerfile" -t armada-web:6.33.3 "$package_root"
# Scheduled Disk jobs must not be claimed while web is stopped: holding only
# backup.lock would allow a job to be claimed and stranded during maintenance.
mkdir -p -- "$backup_root"
chmod 700 -- "$backup_root"
exec 7>"$backup_root/.disk-worker.lock"
flock -n 7 || { printf 'Обработчик резервирования занят. Повторите после его завершения.\n' >&2; exit 1; }
exec 8>"$backup_root/.backup.lock"
flock -n 8 || { printf 'Резервирование занято; обновление не начато.\n' >&2; exit 1; }
# Private recovery state is durable even after an interrupted installer.
recovery_parent=$(dirname -- "$app_root")
recovery=$(mktemp -d "$recovery_parent/.armada-recovery-6.33.3.XXXXXX")
python3 - "$recovery" <<'PY'
import os,stat,sys
st=os.lstat(sys.argv[1])
assert stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode) and st.st_uid==0 and stat.S_IMODE(st.st_mode)==0o700,'recovery должен быть обычным каталогом root с правами 0700'
PY
printf '%s\n' "$installed" > "$recovery/previous-version"
printf '%s\n' "$version_code" > "$recovery/version-check.py"
if [[ -s "$staging/local-files.txt" ]]; then cp -- "$staging/local-files.txt" "$recovery/local-files.txt"; fi
printf 'До миграций: проверить running/images.json/writers/backup.log. Не удалять тома. При неудачном автовозврате вручную остановить писателей и вернуть прежнее дерево/image ID.\n' > "$recovery/RECOVERY.md"
set_phase prepare
# Save exact image IDs for the running services (not mutable tag names).
declared=$("${compose[@]}" --profile max config --services)
previous_services=()
while IFS= read -r service; do
  case "$service" in web|mailworker|maxworker|proxy) previous_services+=("$service");; esac
done <<< "$declared"
"${compose[@]}" --profile max ps --services --status running "${previous_services[@]}" > "$recovery/running"
python3 - "$recovery/running" "$recovery/images.json" "${compose[@]}" <<'PY'
import json,subprocess,sys
services={}
for name in open(sys.argv[1]).read().splitlines():
    if name not in ('web','mailworker','maxworker','proxy'):raise ValueError('Unexpected service')
    cid=subprocess.check_output(sys.argv[3:]+['--profile','max','ps','-q',name],text=True).strip()
    image=subprocess.check_output(['docker','inspect','--format','{{.Image}}',cid],text=True).strip()
    if not image.startswith('sha256:'):raise ValueError('Missing image ID')
    services[name]={'image':image}
with open(sys.argv[2],'x') as f:json.dump({'services':services},f)
PY
printf 'Создаём копию без промежуточного возобновления записи.\n'
bash "$package_root/deploy/beget/backup.sh" --hold-writers --compose-file "$app_root/deploy/beget/compose.yml" --state-file "$recovery/writers" --lock-fd 8 > "$recovery/backup.log" 2>&1
set_phase backup_done
# This checkpoint includes private .env; never upload recovery directories.
python3 "$package_root/deploy/beget/switch_tree.py" prepare --source "$package_root" --app "$app_root" --recovery "$recovery"
python3 - "$recovery" "$app_root" "$(printf '%q ' "${compose[@]}")" <<'PY'
import pathlib,sys
r=pathlib.Path(sys.argv[1]);app=sys.argv[2];compose_cmd=sys.argv[3].strip()
lines=(r/'backup.log').read_text().splitlines()
archives=[line.removeprefix('Backup saved: ') for line in lines if line.startswith('Backup saved: ')]
if len(archives)!=1 or not pathlib.Path(archives[0]).is_file():raise ValueError('Backup missing')
(r/'archive').write_text(archives[0]+'\n')
(r/'RECOVERY.md').write_text("""# Восстановление после неуспешного обновления

Файл phase в этом каталоге содержит последнюю ПОДТВЕРЖДЁННУЮ фазу: prepare, backup_done, switched, migrate_started, migrate_done, verify или complete. Отсутствие фазы migrate_done означает, что завершение миграций не подтверждено.
Каталог содержит закрытые настройки (.env). Не публиковать, не отправлять в облако, не удалять автоматически.

## Общее
1. Остановить web/mailworker/maxworker, таймер и службу armada-disk-backup. Проверить, что посторонних писателей нет.
2. Сохранить текущее состояние отдельно для расследования.

## Путь А. Диагностика и исправление новой версии (ТОЛЬКО если phase = migrate_done или verify)
Одноразовый проверяющий контейнер (run --rm) выполняет одну команду и завершается: у него нет опубликованных портов и фоновых процессов, он не принимает запросы и не запускает почтовый процесс и MAX. Постоянные писатели (up -d web/mailworker) принимают запросы и пишут в базу; их запускают только после успеха ВСЕХ проверок ниже. Команды не печатают конфигурацию и секреты; не запускайте compose config без --quiet.
1. """+compose_cmd+""" config --quiet
2. """+compose_cmd+""" run --rm --no-deps -T --entrypoint python web server/manage.py check
3. """+compose_cmd+""" run --rm --no-deps -T --entrypoint python web server/manage.py migrate --check
4. Ожидаемая версия 6.33.3: """+compose_cmd+""" run --rm --no-deps -T --entrypoint python web -c "$(cat """+str(r)+"""/version-check.py)"
5. Контролируемый запуск: """+compose_cmd+""" up -d --no-build --no-deps web mailworker, затем дождаться статуса healthy у web (docker inspect --format '{{.State.Health.Status}}' для контейнера web), затем """+compose_cmd+""" up -d --no-build --no-deps --force-recreate proxy. MAX запускать только если maxworker указан в файле writers и после отдельной проверки.
Если любая проверка не пройдена или результат неясен, остановиться и перейти к пути Б.

## Путь Б. Согласованное восстановление из предустановочного состояния (основной при phase = migrate_started или неизвестной)
1. Не запускать прежний web поверх изменённой схемы.
2. В файле archive указан точный предустановочный архив. Если он зашифрован, расшифровать на доверенной машине закрытым ключом восстановления; ключ не копировать на VPS.
3. Проверить SHA256SUMS внутри архива и выполнить пробное восстановление в отдельную временную БД/тома, включая файлы и ключи. Инструкция deploy/beget/README.md прежнего дерева и docs/YANDEX_DISK_BACKUPS.md описывает восстановление полного архива. Не выполнять down -v.
4. Восстановить предустановочные БД, config и documents вместе; вернуть дерево tree и прежние image ID из images.json. Только затем запустить web и проверить версию, migrate --check, документы и данные. Включать почту и MAX после отдельной проверки; MAX только если был включён прежде.
5. Файлы local-files.txt (если есть) лежат только в tree; в новую версию автоматически не переносятся.

## Ручная очистка
Автоматического удаления нет. Список комплектов и их состояние: python3 deploy/beget/recovery_inventory.py --directory <каталог рядом с приложением>. Не удалять последний завершённый комплект и комплекты неудачных или незавершённых обновлений. Подробнее: RELEASE_6_33_1.md.

Точное расположение приложения: """+app+'\nТочный архив: '+archives[0]+'\n')
PY
max_was_running=0
while IFS= read -r service; do
  if [[ "$service" == maxworker ]]; then max_was_running=1; fi
done < "$recovery/writers"
switched=1
python3 "$package_root/deploy/beget/switch_tree.py" switch --app "$app_root" --recovery "$recovery"
set_phase switched
cd -- "$app_root"
# Check configuration before crossing the irreversible migration barrier.
"${compose[@]}" config --quiet
set_phase migrate_started
"${compose[@]}" run --rm --no-deps -T --entrypoint python web server/manage.py migrate --noinput
set_phase migrate_done
"${compose[@]}" run --rm --no-deps -T --entrypoint python web server/manage.py check
"${compose[@]}" run --rm --no-deps -T --entrypoint python web server/manage.py migrate --check
set_phase verify
"${compose[@]}" up -d --no-build --no-deps web mailworker
"${compose[@]}" up -d --no-build --no-deps --force-recreate proxy
healthy=0
for attempt in {1..90}; do
  web_id=$("${compose[@]}" ps -q web)
  worker_id=$("${compose[@]}" ps -q mailworker)
  proxy_id=$("${compose[@]}" ps -q proxy)
  if [[ -n "$web_id" && -n "$worker_id" && -n "$proxy_id" ]] &&
     [[ $(docker inspect --format '{{.State.Health.Status}}' "$web_id") == healthy ]] &&
     [[ $(docker inspect --format '{{.State.Running}}' "$worker_id") == true ]] &&
     [[ $(docker inspect --format '{{.State.Running}}' "$proxy_id") == true ]]; then healthy=1; break; fi
  sleep 2
done
if (( healthy == 0 )); then
  "${compose[@]}" ps
  printf 'Не подтверждён запуск сайта, почтового процесса или прокси. MAX-worker не запускался.\n' >&2; exit 1
fi
"${compose[@]}" exec -T web python server/manage.py check
"${compose[@]}" exec -T web python server/manage.py migrate --check
actual=$("${compose[@]}" exec -T web python -c "$version_code")
[[ "$actual" == 6.33.3 ]] || { printf 'Работающий контейнер не подтвердил 6.33.3.\n' >&2; exit 1; }
if (( max_was_running )); then
  "${compose[@]}" --profile max up -d --no-build maxworker
  sleep 3
  max_id=$("${compose[@]}" --profile max ps -q maxworker)
  [[ -n "$max_id" && $(docker inspect --format '{{.State.Running}}' "$max_id") == true ]] || { printf 'MAX-worker не возобновился. Проверьте журнал.\n' >&2; exit 1; }
fi
completed=1
set_phase complete || printf 'Не удалось записать фазу complete; установка при этом проверена.\n' >&2
target_release_version=6.33.3
target_release_doc="RELEASE_${target_release_version//./_}.md"
printf '\nОбновление 6.33.3 установлено. Данные и настройки сохранены.\nКаталог recovery: %s\nВНИМАНИЕ: он содержит закрытые настройки (.env) и копию прежнего дерева. Не публиковать и не загружать в облако. Автоматически не удаляется; порядок ручной очистки: %s.\n' "$recovery" "$target_release_doc"
if (( max_was_running )); then printf 'Ранее работавший MAX-worker возобновлён.\n'; else printf 'MAX-worker не включался автоматически. Настройка и пилот — отдельный шаг.\n'; fi
