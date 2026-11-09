# Запуск на сервере и автоматическое обновление

Для Пети SSL. Репозиторий публичный: https://github.com/ranopashec/conflictogen_bot

## Как работает обновление

Сервер раз в минуту проверяет `main`. Если появился новый коммит, он собирает Docker-образ. В сборку включён обязательный прогон тестов; только после успеха контейнер заменяется новой версией. Скрипт ждёт успешного подключения бота к Telegram и проверяет heartbeat. При неудачном запуске пытается вернуть предыдущий образ. При неудачной сборке работающий контейнер не трогается.

Это pull-деплой: после обычного `git push origin main` обновление начнётся в течение примерно минуты плюс время сборки. Быстрые последовательные пуши могут объединиться в один деплой последнего коммита. Вебхук, открытые порты и SSH-токены в GitHub не нужны. Отдельный GitHub Actions workflow запускает тесты и проверку секретов; сервер также прогоняет тесты сам, не ожидая статуса Actions.

## 1. Подготовить сервер

Нужны Linux с systemd, Git, Docker Engine с Compose v2 (поддержка `up --wait`), `flock` из util-linux и исходящий HTTPS к GitHub, Telegram и LLM-провайдерам. Рекомендуется хотя бы 1 ГБ RAM и запас места для Docker-сборок. Входящие порты не используются. Установи Docker по официальной инструкции для своей ОС: https://docs.docker.com/engine/install/

Проверка:

```sh
git --version
docker version
docker compose version
command -v flock
```

Устанавливается только отдельный проект Compose `conflictogen-bot` и два одноимённых systemd-unit. Чужие проекты и контейнеры скрипт не останавливает.

## 2. Получить конфигурацию приватно

Токен и базу НЕ брать из репозитория: их там нет. До первого старта свяжись с владельцем бота, чтобы он остановил локальную копию и передал конфигурацию по согласованному приватному каналу. Два процесса с одним Telegram-токеном одновременно работать не должны.

На сервере создай файл:

```sh
sudo install -m 600 /dev/null /etc/conflictogen-bot.env
sudoedit /etc/conflictogen-bot.env
```

Содержимое (значения заполняются только на сервере):

```dotenv
TELEGRAM_BOT_TOKEN=
ENCRYPTION_KEY=
ADMIN_IDS=
DAILY_LIMIT=30
```

`ADMIN_IDS` — числовые Telegram ID администраторов через запятую. Никаких ключей Gemini/Groq в серверном конфиге не требуется: участники подключают собственные ключи через личный чат с ботом.

Если переносим существующих пользователей и их ключи, нужен именно прежний `ENCRYPTION_KEY` вместе с прежней базой. Если начинаем с пустой базы, новый ключ можно создать так:

```sh
docker run --rm python:3.13-slim python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Не вставляй секреты в Git, команды с аргументами, публичные логи или issue. Храни конфигурацию отдельно: `/etc/conflictogen-bot.env`, права `600`.

## 3. Установить

Сначала владелец останавливает локальный процесс. Затем:

```sh
git clone https://github.com/ranopashec/conflictogen_bot.git
cd conflictogen_bot
sudo bash deploy/install.sh
```

Установщик сначала выполняет первый деплой. Только после успеха включает таймер. Результат:

- конфигурация: `/etc/conflictogen-bot.env`;
- код и версии: `/var/lib/conflictogen-bot/releases/`;
- зеркало публичного Git: `/var/lib/conflictogen-bot/repo.git`;
- данные: Docker volume `conflictogen-bot_conflict-data`;
- сервис обновления: `conflictogen-bot-update.service`;
- таймер: `conflictogen-bot-update.timer`.

Перезапуски и новые образы не удаляют том с SQLite. Не выполняй `docker compose down -v`.

## 4. При необходимости перенести текущую базу

Этот шаг делается ДО установки/первого запуска, после остановки локальной копии. Попроси владельца передать закрытый файл `data/bot.sqlite3` и его исходный `ENCRYPTION_KEY`. Оба объекта должны идти приватно. Файл базы на сервере положи во временный закрытый каталог; ниже условное имя `/root/transfer/bot.sqlite3`.

```sh
sudo docker volume create conflictogen-bot_conflict-data
sudo docker run --rm --user 0 \
  -v conflictogen-bot_conflict-data:/target \
  -v /root/transfer:/transfer:ro \
  python:3.13-slim sh -c 'cp /transfer/bot.sqlite3 /target/bot.sqlite3 && chown -R 10001:10001 /target && chmod 700 /target && chmod 600 /target/bot.sqlite3'
```

После переноса удали временную копию по своим правилам хранения секретов. Если миграция не нужна, участники просто переподключат ключи; сервер начнёт с пустых личных банков и общего школьного набора из 41 кейса.

## 5. Проверить

```sh
sudo systemctl status conflictogen-bot-update.timer --no-pager
sudo journalctl -u conflictogen-bot-update.service -n 80 --no-pager
sudo docker ps --filter label=com.docker.compose.project=conflictogen-bot
```

В Telegram: https://t.me/conflictogen_bot → `/start`, `/public`, `/add`, затем `/connect` и «Новый конфликт». Администратор проверяет `/review`.

После следующего пуша в `main` журнал должен показать новый SHA и `Deployed commit ...`. Текущий SHA хранится в `/var/lib/conflictogen-bot/current`.

Принудительно проверить обновление:

```sh
sudo systemctl start conflictogen-bot-update.service
```

Поставить автообновление на паузу (сам бот продолжит работать):

```sh
sudo systemctl disable --now conflictogen-bot-update.timer
```

После изменения серверного `.env` контейнер надо пересоздать. Не перегенерируй `ENCRYPTION_KEY` при обновлениях: старые пользовательские ключи станут нечитаемыми.

Откат контейнера не откатывает SQLite-миграции. Перед будущими несовместимыми изменениями схемы нужна отдельная резервная копия. Старые образы и версии автоматически не удаляются; чистку диска администратор делает отдельно.

Скрипт обновления копируется при установке. Если меняется сам `deploy/update.sh` или systemd-unit, повтори `sudo bash deploy/install.sh` из обновлённого клона; изменения приложения подхватываются автоматически.
