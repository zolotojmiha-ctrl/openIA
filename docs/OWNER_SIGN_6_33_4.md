# Подпись выполнена — 19 ноября 2026

Публичный ответ владельца проверен, выпуск опубликован. Эти команды повторять не нужно; следующий шаг docs/INSTALL_6_33_4.md. Ниже история выполненного этапа.

# Подпись кандидата 6.33.4 владельцем

Кандидат проверен настоящей установкой и восстановлением в облаке. Production остаётся подтверждённой владельцем 6.33.3. Следующий шаг — локальная подпись прежним ключом, без Docker, GitHub Environment/Secrets и нового восстановления реальной копии на Mac. Source a32e0f65a42eff04196f86c5ba6e38fcf6851b8d; immutable candidate publication 85154addd21bade986863baa097d4a7434244867. Не пересобирать кандидат после подписи.

Выполнить только в обычном терминале Mac (не в SSH/root):

```bash
bash <<'SIGN'
set -euo pipefail
if [[ "$(uname -s)" != "Darwin" || "$EUID" -eq 0 ]]; then
  echo 'Эту команду нужно выполнить в обычном терминале Mac, не на VPS.' >&2
  exit 1
fi
mkdir -p "$HOME/Downloads"
armada_sign_work=$(mktemp -d "$HOME/Downloads/armada-6.33.4-sign.XXXXXX")
armada_candidate_url='https://raw.githubusercontent.com/zolotojmiha-ctrl/openIA/85154addd21bade986863baa097d4a7434244867/candidates/6.33.4/a32e0f65a42eff04196f86c5ba6e38fcf6851b8d'
for armada_name in openIA-6.33.4-update.zip install-6.33.4.sh openIA-6.33.4-update.json; do
  curl --fail --location --retry 3 --output "$armada_sign_work/$armada_name" "$armada_candidate_url/$armada_name"
done
printf '%s\n' \
 '3d62136fb95675050413658ec15ba9336360ad92888c0e1687dc1feb5b5fe0e4  openIA-6.33.4-update.zip' \
 '9d62f61066e057500039c89affd3dd20bc40a0c356e5adf8a8e04febecd5ec70  install-6.33.4.sh' \
 'abe15f50ec63f92ea30ebd88f46bdfbc514c667519617f89524e1431edef8751  openIA-6.33.4-update.json' \
 | (cd "$armada_sign_work" && shasum -a 256 -c -)
unzip -q "$armada_sign_work/openIA-6.33.4-update.zip" \
 'openIA-feature-accounts-foundation/tools/owner_sign_6334.py' \
 'openIA-feature-accounts-foundation/tools/release_6332_baseline/*' \
 -d "$armada_sign_work/helper"
python3 "$armada_sign_work/helper/openIA-feature-accounts-foundation/tools/owner_sign_6334.py" \
 --directory "$armada_sign_work" \
 --archive-sha 3d62136fb95675050413658ec15ba9336360ad92888c0e1687dc1feb5b5fe0e4 \
 --installer-sha 9d62f61066e057500039c89affd3dd20bc40a0c356e5adf8a8e04febecd5ec70 \
 --source a32e0f65a42eff04196f86c5ba6e38fcf6851b8d
SIGN
```

Выберите прежний armada-release-signing.key.pem; пароль вводится только по запросу OpenSSL. В чат передаётся исключительно armada-6.33.4-public-signatures.zip из пути, который напечатает помощник. Закрытый ключ, пароли, резервная копия и папки с частными материалами не передаются. После проверки публичного ответа агент соберёт и опубликует подписанный пакет и выдаст отдельные SSH/root команды установки. Владелец запускает установщик из GitHub.
