## 19 ноября 2026: подписанная 6.33.4 опубликована, ожидается установка владельцем

Публичный ответ владельца получен, обе подписи и неизменяемый пакет проверены. Публикация downloads/nas-updates b478d99249a1131e9aafb3d3bd4fa49f0d87e72b, source a32e0f65a42eff04196f86c5ba6e38fcf6851b8d. Внешний ZIP SHA-256 1852edaec25a60aec5da5debd4f6f29562af8941e75af0568cb919efb81cac98. Независимое скачивание: внешний подписанный ZIP, внутренний манифест, все суммы и неизменное прежнее restore proof — OK. Production пока остаётся подтверждённой владельцем 6.33.3; подпись повторно не запрашивать и кандидат не пересобирать. Инструкция установки docs/INSTALL_6_33_4.md. VPS агентом не затрагивался; прежние записи об ожидании подписи ниже исторические.

# Продолжение 6.33.4

Production по владельцу: 6.33.3 установлена, данные/настройки сохранены, MAX-worker возобновлён. VPS агентом не затрагивается. Настройки опубликованной среды не менять. Разработка/все испытания только в облаке; Mac только для подписи владельцем; устанавливает владелец из GitHub. Не повторять обучение с GitHub Environments/Secrets, не запрашивать private key или production backup и не запускать новое Mac-восстановление.

## Неизменяемый кандидат

- App source: a32e0f65a42eff04196f86c5ba6e38fcf6851b8d.
- Candidate publication: 85154addd21bade986863baa097d4a7434244867.
- Path: candidates/6.33.4/a32e0f65a42eff04196f86c5ba6e38fcf6851b8d.
- Archive SHA-256: 3d62136fb95675050413658ec15ba9336360ad92888c0e1687dc1feb5b5fe0e4.
- Installer SHA-256: 9d62f61066e057500039c89affd3dd20bc40a0c356e5adf8a8e04febecd5ec70.
- Unsigned metadata SHA-256: abe15f50ec63f92ea30ebd88f46bdfbc514c667519617f89524e1431edef8751.
- Branch: feature/rental-settlements-6.33.4. Позднейшие commits только docs, не менять source кандидата.

Публичный кандидат проверен повторным скачиванием; source/ZIP совпадают, ready_for_install=false, signature_created=false. Не устанавливать его без подписи. Все испытания/границы в docs/RELEASE_6_33_4_VALIDATION.md. Синтетическая тестовая подпись была только в отдельной папке и НЕ опубликована.

## Следующее действие

Владелец выполняет docs/OWNER_SIGN_6_33_4.md на Mac и присылает только armada-6.33.4-public-signatures.zip. Прежний ключ armada-release-signing.key.pem остаётся локально; его DER SPKI SHA-256 e2f6b26419ccb10faa61baf1cfb5a0ea53f499da8803f23f0dd0df3f420aa3e4. Backup public SHA ed88e924d64fa785d343907855b0245b2a01c932cfdb28a44ff3490e77aa31a4. Историческое подписанное доказательство реального восстановления 6.33.1 хранится публично в tools/release_6332_baseline; не менять его дату/содержимое и не объявлять новым восстановлением.

После ответа: python3 tools/assemble_signed_6334.py --candidate /workspace/armada-6334-build/downloaded-candidate --response <public ZIP> --output /workspace/armada-6334-build/owner-signed --source a32e0f65a42eff04196f86c5ba6e38fcf6851b8d. Этот сборщик проверяет прежний доверенный ключ, подпись манифеста, неизменяемые archive/installer/source, прежнее restore proof, точный внешний ZIP и подпись владельца. Папка output новая; повторная запись запрещена. Ничего не пересобирать после подписи.

Публиковать только проверенные armada-6.33.4.zip + sig + sha256 и fixes/release-6.33.4/* на downloads/nas-updates, сохранив все прежние releases/candidates. Затем независимо скачать/проверить подписи и SHA и выдать SSH отдельно от root-команды установки с неизменяемым commit URL. Прежний независимый доверенный public key VPS: /root/armada-6.32.0/armada-release-signing.public.pem; приложение /opt/armada, .env в deploy/beget/.env. SSH команда владельцу: ssh -o UseKeychain=yes -o IdentitiesOnly=yes -o PasswordAuthentication=no -i ~/.ssh/armada_beget root@185.23.35.28. Не выполнять SSH агентом.

## Облачные артефакты

/workspace/armada-6334-build/candidate — исходный неподписанный кандидат; downloaded-candidate — три независимо скачанных файла; publication.json — кандидат commit/path. sign-on-mac.sh — готовая команда. test-signed — только синтетическая тестовая копия, НЕ публиковать. /workspace/rental-6334-*.log — журналы, часть промежуточных неуспешна, итоговые перечислены в протоколе. /workspace/armada-6333-final-stand — использованный синтетический стенд после upgrade/repeat, сейчас остановлен; keys/backups/recovery/receipts не публиковать. Стендовые ключи не являются ключами владельца. Dev PostgreSQL отдельно на 55432 продолжает работать.

## Использование после production установки

Не создавать второй платёж вместо разнесения уже существующего. В истории исходного платежа разнести его на услуги и возвратный залог, сохранив сумму перечисления. Залог не погашает услуги автоматически. В сверке указывается полная стоимость услуг до зачёта аванса, а не только остаток счёта к доплате. Факт оплаты подтверждается отдельным платежом и привязкой к сверке; получение PDF не доказывает перевод. Старые закрытые табели не конвертируются автоматически: для пересекающейся новой сверки нужно явно открыть прежний период, снимок остаётся.
