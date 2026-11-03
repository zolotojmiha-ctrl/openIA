#!/usr/bin/env bash
# Install only after the existing VPS and its production data have been checked.
set -euo pipefail
app_root=${ARMADA_APP_ROOT:-/opt/armada/openIA-feature-accounts-foundation}
if (( EUID != 0 )); then printf 'Запустите от root.\n' >&2; exit 1; fi
if [[ ! "$app_root" =~ ^/[a-zA-Z0-9_./-]+$ || ! -f "$app_root/deploy/beget/disk-backups.sh" || ! -f "$app_root/deploy/beget/compose.yml" ]]; then
  printf 'Не найден существующий каталог приложения.\n' >&2; exit 1
fi
command -v systemctl >/dev/null
cat > /etc/systemd/system/armada-disk-backup.service <<EOF
[Unit]
Description=ARMADA encrypted Yandex Disk backup worker
After=docker.service network-online.target
Requires=docker.service
[Service]
Type=oneshot
User=root
WorkingDirectory=$app_root
ExecStart=/usr/bin/bash $app_root/deploy/beget/disk-backups.sh
TimeoutStartSec=infinity
UMask=0077
EOF
cat > /etc/systemd/system/armada-disk-backup.timer <<'EOF'
[Unit]
Description=Check ARMADA backup queue every five minutes
[Timer]
OnBootSec=2min
OnUnitInactiveSec=5min
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now armada-disk-backup.timer
printf 'Обработчик подключён. Проверьте его состояние в ЛК через несколько минут. Расписание включается отдельно в ЛК.\n'
