# Telerixa

Telerixa пересылает посты из Telegram-каналов в Discord через webhook: текст, фото, видео, альбомы и часть контекста ответов/форвардов.

Текущая версия: `0.1.0`

## Возможности

- Несколько Telegram-каналов.
- Настройки через `config.json` и web UI без перезапуска бота.
- SQLite-состояние для last seen, отправленных сообщений и retry-очереди.
- Очередь повторной отправки при сетевых ошибках.
- Лимит размера файлов Discord и стратегия для больших видео.
- Настраиваемый catch-up хвост после простоя.
- Логи в консоль и в `logs/bot.log`.
- Автодеплой на Steam Deck по SSH без перетирания runtime-файлов.

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

## Деплой на Steam Deck

Скопируй пример локального deploy-конфига:

```bat
copy deploy_config.example.bat deploy_config.local.bat
```

Заполни `deploy_config.local.bat`, затем запускай:

```bat
deploy_to_deck.bat
```

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
