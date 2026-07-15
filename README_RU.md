# Telerixa

Telerixa пересылает посты из Telegram-каналов в Discord через webhook: текст, фото, видео, альбомы и часть контекста ответов/форвардов.

Текущая версия: `0.3.0`

## Возможности

- Несколько Telegram-каналов.
- Параллельная проверка каналов с последующей отправкой общей ленты в хронологическом порядке.
- Полный контекст ответов и форвардов без искусственного обрезания.
- Настройки через `config.json` и web UI без перезапуска бота.
- Атомарная горячая перезагрузка конфигурации.
- SQLite-состояние для last seen, отправленных сообщений и retry-очереди.
- Надёжная retry-очередь с сохранением прогресса и продолжением после перезапуска.
- Раздельная обработка временных и неустранимых ошибок: один проблемный пост не блокирует остальные.
- Лимит размера файлов Discord и стратегия для больших видео.
- Настраиваемый catch-up хвост после простоя.
- Логи в консоль и в `logs/bot.log`.
- JSON-локализация с русским и английским каталогами.
- Автодеплой на Steam Deck по SSH с обязательным прогоном тестов и без перетирания runtime-файлов.

## Установка

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

На SteamOS/Linux удобнее запускать через `run.sh`: он сам создаст Linux-venv при необходимости.

## Конфигурация

Скопируй пример:

```bat
copy config.example.json config.json
```

Заполни в `config.json`:

- `DISCORD_WEBHOOK_URL`
- `DISCORD_ALERT_USER_ID`, если нужны личные уведомления об аварийном падении
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_CHANNELS`
- `LANGUAGE`, например `ru` или `en`
- `DISCORD_FILE_LIMIT_MB`
- `LARGE_FILE_ACTION`
- `STARTUP_CATCH_UP_LIMIT`
- `MAX_QUEUE_ATTEMPTS`

Реальный `config.json` не должен попадать в Git.

## Запуск

Windows:

```bat
run.bat
```

SteamOS/Linux:

```bash
chmod +x run.sh run_ui.sh
./run.sh
```

Web UI:

```bat
run_ui.bat
```

или на SteamOS/Linux:

```bash
./run_ui.sh
```

## Тесты

Для запуска регрессионных тестов установи dev-зависимости.

Windows:

```bat
.venv\Scripts\pip install -r requirements-dev.txt
```

SteamOS/Linux:

```bash
.venv-linux/bin/pip install -r requirements-dev.txt
```

Запуск:

```bash
python -m pytest
```

Без pytest можно запустить через стандартную библиотеку:

```bash
python -W error::ResourceWarning -m unittest discover -s tests -v
```

## Деплой на Steam Deck

Скопируй пример локального deploy-конфига:

```bat
copy deploy_config.example.bat deploy_config.local.bat
```

Заполни `deploy_config.local.bat`, затем запускай:

```bat
deploy_to_deck.bat
```

Сначала deploy-скрипт запускает полный набор локальных тестов. Если хотя бы один тест падает, деплой останавливается до первого SSH-подключения и Steam Deck не изменяется.

Деплой копирует только кодовые файлы. Runtime-файлы на деке не трогает: `config.json`, `bot_state.db`, `tg_session.session`, `logs/`.

## Git

Перед первым коммитом смотри [GIT_SETUP.md](GIT_SETUP.md).

Нельзя коммитить:

- `config.json`
- `deploy_config.local.bat`
- `bot_state.db`
- `tg_session.session`
- `seen_messages.json`
- `logs/`
- `.venv/`, `.venv-linux/`

## Полезные ссылки

- Telethon docs: https://docs.telethon.dev/
- Discord webhooks: https://discord.com/developers/docs/resources/webhook
- Telegram API: https://my.telegram.org/
