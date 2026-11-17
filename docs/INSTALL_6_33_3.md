# Установка подписанной 6.33.3

Выпуск подписан владельцем и опубликован: downloads/nas-updates, коммит b9472e5a0c4f7a2174ac22c84f03093189297c16. Источник приложения — 80f88912b4dc2ea1677138c69ba4b2c3c03a2c10. Повторное скачивание подтвердило обе подписи, манифест, все суммы, источник и неизменённое прежнее подтверждение восстановления. Установка на рабочий сервер пока не подтверждена; последнее подтверждение владельца — 6.33.2.

Владелец сначала подключается к VPS отдельной SSH-командой из своего Mac. Следующий блок выполнять только в появившемся терминале root на VPS. Он проверяет прежний ключ, внешний пакет и внутренние файлы; затем запускает штатный установщик с шифрованием свежей копии нынешних данных. Старый backup скачивать на Mac не нужно.

```bash
bash <<'UPDATE'
set -euo pipefail

if [[ "$(uname -s)" != "Linux" || "$EUID" -ne 0 ]]; then
  echo 'Эту команду нужно выполнить на VPS от root, после подключения по SSH.' >&2
  exit 1
fi

test -f /opt/armada/deploy/beget/compose.yml
armada_trust='/root/armada-6.32.0/armada-release-signing.public.pem'
test -f "$armada_trust"
armada_fingerprint=$(openssl pkey -pubin -in "$armada_trust" -outform DER | sha256sum | awk '{print $1}')
[[ "$armada_fingerprint" == 'e2f6b26419ccb10faa61baf1cfb5a0ea53f499da8803f23f0dd0df3f420aa3e4' ]]

armada_work=$(mktemp -d /root/armada-6.33.3.XXXXXX)
armada_url='https://raw.githubusercontent.com/zolotojmiha-ctrl/openIA/b9472e5a0c4f7a2174ac22c84f03093189297c16'

curl --fail --location --retry 3 --output "$armada_work/armada-6.33.3.zip" "$armada_url/armada-6.33.3.zip"
curl --fail --location --retry 3 --output "$armada_work/armada-6.33.3.zip.sig" "$armada_url/armada-6.33.3.zip.sig"

printf '%s\n' '57ae4f99184183b8b12c5ffe6a0524987fca8328d1d566f3ebcc321ec0e70a88  armada-6.33.3.zip' | (cd "$armada_work" && sha256sum -c -)
openssl dgst -sha256 -verify "$armada_trust" -signature "$armada_work/armada-6.33.3.zip.sig" "$armada_work/armada-6.33.3.zip"

unzip -q "$armada_work/armada-6.33.3.zip" -d "$armada_work"
armada_release="$armada_work/armada-6.33.3"
(
  cd "$armada_release"
  openssl dgst -sha256 -verify "$armada_trust" -signature SHA256SUMS-6.33.3.sig SHA256SUMS-6.33.3
  sha256sum -c SHA256SUMS-6.33.3
  printf '%s\n' 'ed88e924d64fa785d343907855b0245b2a01c932cfdb28a44ff3490e77aa31a4  backup-recovery.public.pem' | sha256sum -c -
)

ARMADA_APP_ROOT=/opt/armada \
ARMADA_RELEASE_PUBLIC_KEY="$armada_trust" \
ARMADA_BACKUP_PUBLIC_KEY="$armada_release/backup-recovery.public.pem" \
bash "$armada_release/install-6.33.3.sh"
UPDATE
```

Пришлите завершающий вывод установщика. Не присылайте .env, ключи, копию данных или каталог recovery. После успешной установки — пилот MAX/ЛК по docs/CONTINUE_6_33_3.md. Два старых падения сценариев подрядчиков описаны в протоколе, реальный MAX ещё требует пилота.
