# Ежедневная наработка: MAX, 20:00 Омск

Реализован независимый от почты планировщик в MAX-worker, идемпотентная очередь и повторная проверка перед доставкой. Ссылка выбирает конкретную дату в существующей форме техники; прежнее поведение выбора самого раннего пропуска при обычном открытии сохранено. Денежные формы и способы расчёта не менялись. Требования и ограничения: requirements/MAX_RENTAL_REMINDERS_2026_11_13.md.

Проверки: 141 тест SQLite, включая существующие MAX, наработку, права механика и Chromium сохранения тарифа/аванса — OK, 2 PostgreSQL-теста пропущены. 67 тестов PostgreSQL — OK, 1 пропущен (тот же read-only сценарий отдельно выполнен через TransactionTestCase); подтверждены конкурентная постановка ровно одного задания и статус в READ ONLY. После добавления проверки ответственности через закрепление и пояснения выбранной даты: 16 тестов SQLite/Chromium — OK, 2 PostgreSQL-теста пропущены. Браузер открывает ссылку напоминания при более ранних пропусках, видит сегодняшний день, сохраняет 10 часов именно на него; проверены ширины 320/390/1440. Реальные MAX API, аккаунты и VPS из среды разработки не использовались.

Новых миграций и изменений прав/закреплений/старых отметок нет. Установщик проверяет исходные хэши, сохраняет резерв БД и исходников, заменяет только перечисленные файлы, пересобирает web и перезапускает ранее работавший MAX-worker. Ранее выключенный MAX не включается сам. Отправку конкретному мастеру на VPS подтверждать только после установки и диагностики, не по результатам локальных тестов.

Пакет включает rental_views.py целиком: старый обязательный хэш ffff3c425b1b3190e12477d89d7bcc4a94039b4603903b0ec85399d3af249bc2 проверяется как before в манифесте, новый хэш 66ad06efad11708b4cc8b2e10f2d02b9b0844fb0d23ff5a47201383b65ec712e нужен будущим установщикам. Старые опубликованные пакеты не переписываются.

Подписанный ZIP опубликован в ce64d2e9c6d1d2ba29c63dc4f64c9c56e0e6ab60, исходники 1930a3c3a7346cc76827db39f4a5352071f1820f. SHA-256: 0c69bf959201885069e4270d258494efe4f75b2a16b05af586bc2883cc3016cb. ZIP/подпись повторно скачаны по HTTPS, хэш совпал, Verified OK. Установщик в обоих режимах MAX проверен: отказ на изменённом исходнике до резервирования, установка/повтор, сохранение посторонних данных и резервов. Docker-кандидат 2ca2fad5619b36ee7b85937c6866645a6c5262bef19cd97ab064f7f6880c8aee: 15 профильных тестов OK (2 PostgreSQL-пропуска), пять runtime-файлов сверены с комплектом. Первая попытка тестов контейнера прервана из-за временной нехватки места; повтор с read-only root и tmpfs завершился успешно. Установка на VPS и фактическая отправка не выполнялись разработчиком.

Подключение с Mac:

```bash
ssh -o UseKeychain=yes -o IdentitiesOnly=yes -o PasswordAuthentication=no -i ~/.ssh/armada_beget root@185.23.35.28
```

Установка на VPS:

```bash
cd /root &&
ARMADA_FIX_URL='https://raw.githubusercontent.com/zolotojmiha-ctrl/openIA/ce64d2e9c6d1d2ba29c63dc4f64c9c56e0e6ab60' &&
curl -fL --retry 3 -o armada-max-rental-reminders.zip "$ARMADA_FIX_URL/armada-max-rental-reminders.zip" &&
curl -fL --retry 3 -o armada-max-rental-reminders.zip.sig "$ARMADA_FIX_URL/armada-max-rental-reminders.zip.sig" &&
echo '0c69bf959201885069e4270d258494efe4f75b2a16b05af586bc2883cc3016cb  armada-max-rental-reminders.zip' | sha256sum -c - &&
openssl dgst -sha256 -verify /root/armada-6.32.0/armada-release-signing.public.pem -signature armada-max-rental-reminders.zip.sig armada-max-rental-reminders.zip &&
python3 -m zipfile -e armada-max-rental-reminders.zip /root &&
ARMADA_APP_ROOT=/opt/armada bash /root/armada-max-rental-reminders/install.sh
```

После установки проверьте статус (только чтение):

```bash
cd /opt/armada && docker compose --env-file deploy/beget/.env -f deploy/beget/compose.yml exec -T web python server/manage.py max_rental_status
```

Если обновление установлено после 20:00, напоминание за сегодня может уйти сразу после запуска MAX-worker, только при незаполненном дне и актуальной ответственности/привязке.
