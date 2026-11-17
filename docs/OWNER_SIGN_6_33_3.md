> Шаг выполнен: публичный ответ владельца проверен, выпуск опубликован. Повторно не подписывать. Следующий шаг — [установка на VPS](INSTALL_6_33_3.md).

# Подпись кандидата 6.33.3 владельцем

Кандидат проверен в облаке и размещён в отдельном каталоге GitHub. Он ещё не подписан и не установлен на рабочий сервер. Следующий шаг выполняется **в обычном терминале Mac, без SSH**. Docker и восстановление резервной копии здесь не используются.

Запустите команду целиком. После сверки трёх SHA-256 откроется выбор файла: выберите прежний `armada-release-signing.key.pem`. Парольную фразу вводите только в запрос OpenSSL в своём терминале. Проверяется прежний отпечаток ключа; другой ключ не подходит.

```bash
bash <<'SIGN'
set -euo pipefail
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo 'Этот шаг выполняется в обычном терминале Mac, без подключения к VPS.' >&2
  exit 1
fi
mkdir -p "$HOME/Downloads"
armada_sign_work=$(mktemp -d "$HOME/Downloads/armada-6.33.3-sign.XXXXXX")
armada_candidate_url='https://raw.githubusercontent.com/zolotojmiha-ctrl/openIA/25ac6375409eedbcda6d34fe42d4b1041ff653db/candidates/6.33.3/80f88912b4dc2ea1677138c69ba4b2c3c03a2c10'
for armada_file in openIA-6.33.3-update.zip install-6.33.3.sh openIA-6.33.3-update.json; do
  curl --fail --location --retry 3 --output "$armada_sign_work/$armada_file" "$armada_candidate_url/$armada_file"
done
printf '%s\n' \
  '6d9f16b560cdfc3fc9cf960556568464d822e6395108764bf8d3673d97102f29  openIA-6.33.3-update.zip' \
  '47158c297ee68075781dde5831085486a5e7283025068c61136bc95a4d4d4f2b  install-6.33.3.sh' \
  '46241583b830022c9c44d8050e82e106d73f1f932fbf6e0559b6b9847d753857  openIA-6.33.3-update.json' \
  | (cd "$armada_sign_work" && shasum -a 256 -c -)
unzip -q "$armada_sign_work/openIA-6.33.3-update.zip" \
  'openIA-feature-accounts-foundation/tools/owner_sign_6333.py' \
  'openIA-feature-accounts-foundation/tools/release_6332_baseline/*' \
  -d "$armada_sign_work/helper"
python3 "$armada_sign_work/helper/openIA-feature-accounts-foundation/tools/owner_sign_6333.py" \
  --directory "$armada_sign_work" \
  --archive-sha 6d9f16b560cdfc3fc9cf960556568464d822e6395108764bf8d3673d97102f29 \
  --installer-sha 47158c297ee68075781dde5831085486a5e7283025068c61136bc95a4d4d4f2b \
  --source 80f88912b4dc2ea1677138c69ba4b2c3c03a2c10
SIGN
```

После успешного завершения помощник выведет фактический путь к `armada-6.33.3-public-signatures.zip`. Пришлите в чат **только этот небольшой публичный ZIP**. Закрытый ключ, парольную фразу, backup, `.env` и каталог recovery не передавайте.

Далее агент проверяет публичный ответ через `tools/assemble_signed_6333.py`, воспроизводит внешний пакет, проверяет подписи и контрольные суммы, размещает уже подписанный выпуск в GitHub и выдаёт отдельные команды подключения и установки на VPS. До этого рабочая версия остаётся 6.33.2.

Два старых падения тестов подрядчиков воспроизведены на 6.33.2; они описаны в протоколе и не считаются исправленными этим выпуском. Реальное отображение в приложении MAX подтверждается отдельным пилотом после установки.
