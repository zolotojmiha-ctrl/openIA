# Установка подписанной 6.33.4

Подписи и повторное скачивание проверены. Источник приложения a32e0f65a42eff04196f86c5ba6e38fcf6851b8d, immutable release commit b478d99249a1131e9aafb3d3bd4fa49f0d87e72b. Установка выполняется владельцем; агент на VPS не подключается.

1. В терминале Mac подключиться:

```bash
ssh -o UseKeychain=yes -o IdentitiesOnly=yes -o PasswordAuthentication=no -i ~/.ssh/armada_beget root@185.23.35.28
```

2. После появления root@…# выполнить:

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

armada_work=$(mktemp -d /root/armada-6.33.4.XXXXXX)
armada_url='https://raw.githubusercontent.com/zolotojmiha-ctrl/openIA/b478d99249a1131e9aafb3d3bd4fa49f0d87e72b'

curl --fail --location --retry 3 --output "$armada_work/armada-6.33.4.zip" "$armada_url/armada-6.33.4.zip"
curl --fail --location --retry 3 --output "$armada_work/armada-6.33.4.zip.sig" "$armada_url/armada-6.33.4.zip.sig"

printf '%s\n' '1852edaec25a60aec5da5debd4f6f29562af8941e75af0568cb919efb81cac98  armada-6.33.4.zip' | (cd "$armada_work" && sha256sum -c -)
openssl dgst -sha256 -verify "$armada_trust" -signature "$armada_work/armada-6.33.4.zip.sig" "$armada_work/armada-6.33.4.zip"

unzip -q "$armada_work/armada-6.33.4.zip" -d "$armada_work"
armada_release="$armada_work/armada-6.33.4"
(
  cd "$armada_release"
  openssl dgst -sha256 -verify "$armada_trust" -signature SHA256SUMS-6.33.4.sig SHA256SUMS-6.33.4
  sha256sum -c SHA256SUMS-6.33.4
  printf '%s\n' 'ed88e924d64fa785d343907855b0245b2a01c932cfdb28a44ff3490e77aa31a4  backup-recovery.public.pem' | sha256sum -c -
)

ARMADA_APP_ROOT=/opt/armada \
ARMADA_RELEASE_PUBLIC_KEY="$armada_trust" \
ARMADA_BACKUP_PUBLIC_KEY="$armada_release/backup-recovery.public.pem" \
bash "$armada_release/install-6.33.4.sh"
UPDATE
```

Установщик создаст свежую зашифрованную копию текущих данных, сохранит deploy/beget/.env и возобновит ранее работавший MAX-worker. После успеха прислать последние строки. Recovery содержит закрытые настройки, остаётся только на VPS; ручная очистка по RELEASE_6_33_4.md. Ранее существующий смешанный платёж нужно разнести, а не создавать заново. Счёт не считать оплаченным без подтверждённого перечисления.
