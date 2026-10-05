# Operations: Ubuntu 24.04 ARM64

Целевой сервер пока не подключён; команды ниже — инструкции установки, а не
отчёт о выполненном deployment. Выберите systemd **или** Docker, не оба одновременно.

## Автоматическая новая установка на Oracle

На Ubuntu 24.04 ARM64/x86_64, как обычный пользователь с sudo:
Публичный репозиторий клонируется без GitHub authentication.

```bash
sudo apt-get update && sudo apt-get install -y git && git clone https://github.com/DaniilLuchkin/cumulative-agent.git && sudo bash cumulative-agent/deploy/install-ubuntu.sh
```

Если репозиторий уже клонирован, достаточно:

```bash
sudo bash cumulative-agent/deploy/install-ubuntu.sh
```

Для source archive без GitHub: перенесите `cumulative-agent-improved.tar.gz` на
сервер и выполните `tar -xzf cumulative-agent-improved.tar.gz`, затем ту же команду
`sudo bash cumulative-agent/deploy/install-ubuntu.sh`. Установка из архива не
требует GitHub authentication и не копирует `.env`/БД/local config.

Ввод Telegram/OpenRouter secrets скрыт. Установщик копирует только tracked Git
files (либо явный список исходников из готового source archive) в `/opt/cumulative-agent`,
создаёт venv, env/config и systemd unit. Worker
и бот работают в одном процессе; БД и owner binding сохраняются через StateDirectory.
Установщик отказывается перезаписывать существующий сервис. Дефолтные модели
выключены, paid budget = 0, ключ OpenRouter сам по себе не включает платные вызовы.

Откройте TCP 80 и 443 в **Oracle VCN Security List или NSG** и в host firewall.
HTTPS может работать **по публичному IPv4 без домена**: Certbot ≥5.4 поддерживает
webroot IP issuance, Let's Encrypt profile `shortlived`. Скрипт спросит email и
согласие на условия CA, создаст/использует бесплатный ACME account. IP сертификата
попадает в публичные certificate transparency logs. Сертификат выдаётся только
после проверки доступа к `http://IP/.well-known/acme-challenge/...` снаружи.
80 должен оставаться доступным для автоматических renewals.

Приложение слушает 127.0.0.1:8000, Nginx публикует HTTPS на 443. HTTP обслуживает
ACME challenges и redirect; до получения сертификата API по HTTP не публикуется.
На ARM64 целевом сервере необходимо проверить фактический запуск и доступность
публичных портов; скрипт не изменяет правила Oracle/firewall.

При ошибке HTTPS установка запускает только Telegram/loopback интерфейс.
После исправления доступа к портам:

```bash
sudo bash /opt/cumulative-agent/deploy/enable-ip-https.sh ВАШ_ПУБЛИЧНЫЙ_IPV4
```

Эта команда также подходит при смене IP: обновляет certificate и Web App URL.
Другие Nginx virtual hosts не удаляются. Она использует выделенные site/lineage,
не перезаписывает unmanaged `cumulative-agent-ip` site. Выделенный Certbot venv
отдельно от приложения. Renewal timer вызывает `certbot renew` дважды в сутки;
deploy hook перезагружает Nginx только после успешного продления.

```bash
sudo systemctl status cumulative-agent
sudo journalctl -u cumulative-agent -f
sudo systemctl list-timers cumulative-agent-certbot.timer
sudo journalctl -u cumulative-agent-certbot.service
```

После запуска **сразу напишите `/start` своему боту**. Первый private text update,
полученный ботом, привязывает owner ID атомарно в SQLite; групповые сообщения,
боты и запросы браузера владельца не назначают. Следующие пользователи игнорируются.
Не публикуйте имя бота до привязки; если бот использовался ранее, первым может
оказаться накопленный Telegram update. Для существующего публичного бота задайте
`TELEGRAM_OWNER_IDS` явно вместо первого сообщения.

Для смены/восстановления владельца без удаления БД:

```bash
sudoedit /etc/cumulative-agent.env
# TELEGRAM_OWNER_IDS=ВАШ_ЧИСЛОВОЙ_ID
# TELEGRAM_CLAIM_FIRST_USER=false
sudo systemctl restart cumulative-agent
```

Для LLM включите разрешённую модель и проверенные цены/бюджеты:

```bash
sudoedit /etc/cumulative-agent.yaml
sudo systemctl restart cumulative-agent
```

Обычный браузер использует `AGENT_API_TOKEN` из root-only env, внутри Telegram
авторизация автоматическая. Если установка остановилась до конца, сохраните
созданные данные/конфиги и выясните причину по выводу; повторный fresh-install
не затирает частично созданный `/opt` или secrets. Не удаляйте БД ради переустановки.

Источники поддержки IP: [Certbot 5.3](https://github.com/certbot/certbot/releases/tag/v5.3.0),
[Certbot 5.4 webroot](https://github.com/certbot/certbot/releases/tag/v5.4.0).

## Systemd

Установите Python 3.12, venv, Git, bubblewrap и util-linux штатными пакетами Ubuntu. Скопируйте этот
проект в `/opt/cumulative-agent`; не заменяйте существующее дерево без проверки.
Создайте `.venv`, установите `requirements.lock`, затем `pip install --no-deps .`.
Код и venv доступны service user на чтение/исполнение; данные отдельно.

Скопируйте `config/default.yaml` в `/etc/cumulative-agent.yaml`,
`deploy/cumulative-agent.service` в `/etc/systemd/system/`.
Создайте `/etc/cumulative-agent.env` с режимом 0600, owner root:

```ini
AGENT_API_TOKEN=<long-random-token>
OPENROUTER_API_KEY=<optional>
TELEGRAM_BOT_TOKEN=<optional>
TELEGRAM_OWNER_IDS=<private-owner-user-ids>
TELEGRAM_CLAIM_FIRST_USER=false
TELEGRAM_WEBAPP_URL=https://tasks.your-domain.com/app
```

Файлы `/etc` требуют root. После настройки:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cumulative-agent
sudo systemctl status cumulative-agent
sudo journalctl -u cumulative-agent -f
```

DynamicUser + StateDirectory создают изолированный writable `/var/lib/cumulative-agent`
и режим 0700. Production directories доступны только для чтения, ProtectHome
скрывает домашние каталоги. Рекомендуемый путь проектов `/srv/projects`; явно
разрешите чтение нужных каталогов сервисному пользователю без world-readable secrets.
Приложение не требует права записи в проекты. Патчи создаются в отдельном snapshot
и worktree внутри StateDirectory; LLM не получает shell. Тесты запускаются только
через bubblewrap, без сети и secrets, с read-only деревом. Если Ubuntu AppArmor или
user namespaces запрещают sandbox, отчёт покажет «не проверено»; обхода нет.
Opt-in, curated test runtime и ручное применение: [PROJECT_WORKFLOWS.md](PROJECT_WORKFLOWS.md).

Dashboard остаётся loopback. Удобный доступ:

```bash
ssh -L 8000:127.0.0.1:8000 user@your-server
```

Для публичного web endpoint нужен TLS reverse proxy с дополнительным access control;
самостоятельно firewall/публикация здесь не меняются.

Для Telegram Mini App HTTPS endpoint должен быть доступен с телефона. Пример domain proxy
в `deploy/nginx-miniapp.conf.example`; инструкция настройки URL/menu button и
signed Telegram auth в [TELEGRAM_MINI_APP.md](TELEGRAM_MINI_APP.md). Дополнительный
reverse-proxy login не должен блокировать загрузку Mini App из Telegram; API
всегда требует signed owner initData или bearer token. Старый мониторинг теперь
по `/dashboard`, интерфейс задач — `/app` и `/`.

## Docker Compose

Image использует официальный multi-arch Python 3.12 и не требует x86 extensions.
Проверка ARM64 build/run на целевом host ещё обязательна.

```bash
cp config/default.yaml config/local.yaml
cp .env.example .env
# Edit secrets and model/budget config locally; do not commit .env.
chmod 600 .env
mkdir -p data
sudo chown 10001:10001 data
chmod 700 data
docker compose build
docker compose up -d
docker compose logs -f agent
```

Host порт только 127.0.0.1; внутри container binding 0.0.0.0 явно разрешён.
Read-only root, no capabilities, CPU/RAM limits, log rotation. Для проектов добавьте
read-only bind mounts и соответствующие внутренние `project_roots`. Secret .env
не включается в build context. Не ставьте quotas выше действительно разрешённых.

Git и bubblewrap включены в image. Docker seccomp/user namespaces могут запрещать
вложенный sandbox: тогда tests остаются `unavailable`. Не используйте `privileged`
ради тестов; для такого host выполните ручную проверку patch в подходящем executor.

При build в managed environment за HTTP proxy используйте доверенный CA bundle:
`docker build --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt -t cumulative-agent .`.
Для runtime с таким proxy смонтируйте combined CA bundle read-only и задайте
`SSL_CERT_FILE` (HTTPX); TLS verification никогда не отключать. На обычном Ubuntu
такой mount не нужен.

## Backup and restore

Обновление существующей установки выполняется отдельно от fresh installer.
Сначала сделайте backup БД и сохраните текущие code/venv/config, затем остановите
сервис, установите проверенные исходники и wheel/dependencies и запустите его снова.
Сохраните `/etc/cumulative-agent.env`, YAML и StateDirectory. Новые таблицы создаются
автоматически; для coding настройте `max_steps: 8` и project opt-in. Старый executable
не поддерживает новые handlers/statuses: rollback требует сохранённого backup.

SQLite online backup API корректно включает WAL:

```bash
AGENT_DATABASE=data/agent.sqlite3 .venv/bin/cumulative-agent backup --output /path/to/new-backup.sqlite3
```

Backup destination должен быть новым; существующий файл не перезаписывается.
При systemd DynamicUser выполняйте backup через управляемый service context или
остановите сервис и скопируйте полный DB/WAL/SHM набор как root. Не делайте `cp`
одного live SQLite файла, пока worker пишет.

Для restore остановите сервис, сохраните текущий DB/WAL/SHM отдельно, скопируйте
проверенный backup в чистый каталог с нужными permissions, запустите сервис.
Не перетирайте исходные файлы без резервной копии. После восстановления возможны
повторные Telegram acknowledgement/results; task idempotency и conservative
reservations защищают очередь и стоимость, не обеспечивают exactly-once remote API.

## Operational acceptance

Проверьте private owner chat, task creation/result delivery, реальные OpenRouter
usage и цены на одной явно ограниченной задаче, API-key cap, service restart,
disk/RAM pressure, backup integrity и восстановление. Не включайте дорогие
фоновые модели до получения статистики качества и проверки quotas.
