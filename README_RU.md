# Telerixa

Telerixa пересылает посты из Telegram-каналов в Discord через webhook: текст, фото, видео, альбомы, native rich-сообщения и контекст ответов/форвардов.

Текущая версия: `0.4.0`

## Возможности

- Несколько Telegram-каналов.
- Параллельная проверка каналов с последующей отправкой общей ленты в хронологическом порядке.
- Полный контекст ответов и форвардов без искусственного обрезания.
- Преобразование native rich-сообщений Telegram в совместимые с Discord заголовки, стили текста, списки, чек-листы, цитаты, код, таблицы, формулы, details-блоки, карты и упорядоченные медиафайлы.
- Сохранение текстовых Telegram-спойлеров в постах, подписях и ответах через разметку Discord.
- Настройки через `config.json` и web UI без перезапуска бота.
- Атомарная горячая перезагрузка конфигурации.
- SQLite-состояние для last seen, отправленных сообщений и retry-очереди.
- Надёжная retry-очередь с сохранением прогресса и продолжением после перезапуска.
- Постоянный архив недоставленных сообщений с повторной постановкой в очередь, скрытием и ссылками на Telegram.
- Понятный статус ручного retry: повторная кнопка блокируется, пока бот не обработает запрос.
- Раздельная обработка временных и неустранимых ошибок: один проблемный пост не блокирует остальные.
- Живой operational dashboard с heartbeat, состоянием очереди, позициями каналов и действиями над ошибками.
- Диагностика по запросу для SQLite, доступа к Discord webhook, Telegram-сессии, диска и FFmpeg.
- Лимит размера файлов Discord и стратегия для больших видео.
- Настраиваемый catch-up хвост после простоя.
- Цветовая индикация логов в консоли и web UI, при этом файлы логов остаются обычным текстом.
- JSON-локализация с русским и английским каталогами.
- Защита от одновременного запуска нескольких экземпляров бота или web UI.
- Привязка процесса к консоли с корректным завершением и очисткой PID-файлов.
- Автодеплой на Steam Deck по SSH с обязательным прогоном тестов и без перетирания runtime-файлов.

## Как это работает

1. Telegram-каналы проверяются параллельно через одну авторизованную Telethon-сессию.
2. Новые посты объединяются в общую хронологическую ленту, а альбомы дедуплицируются в одну единицу доставки.
3. Текст, ответы, спойлеры, rich-блоки и медиа преобразуются в совместимые с Discord payload'ы.
4. Отправка в Discord выполняется последовательно для сохранения порядка. Подтверждённый прогресс сохраняется после каждого медиафайла или текстового chunk'а.
5. Исправимые ошибки попадают в SQLite-очередь, а неисправимые или исчерпавшие retry сообщения уходят в постоянный архив и не блокируют новые посты.

## Web UI

По умолчанию локальный UI слушает `127.0.0.1:8765` и содержит:

- Overview с состоянием процесса, heartbeat, метриками, позициями каналов, retry-очередью и архивом ошибок;
- настройки с валидацией и атомарной горячей перезагрузкой;
- ручной retry, возврат из архива, скрытие и очистку очереди;
- системную диагностику и цветные live-логи без вывода webhook на Overview.

Диагностика проверяет доступность FFmpeg как задел под будущую конвертацию видео. Telerixa `0.4.0` пока не вызывает FFmpeg при доставке.

## Надёжность

Состояние очереди, отправленных сообщений и позиций каналов хранится в SQLite, поэтому перезапуск не теряет накопленный прогресс.

В Windows и SteamOS процесс Telerixa привязан к запустившей его консоли. Закрытие окна завершает соответствующий бот или web UI, а PID-lock не позволяет случайно поднять второй экземпляр и оставить порт `8765` занятым скрытым процессом.

## Структура проекта

| Путь | Ответственность |
| --- | --- |
| `telerixa.py` | Оркестрация приложения, Telegram-сессия, polling и обработка очереди |
| `web_ui.py` | Локальная конфигурация и operational dashboard |
| `telerixa_core/config.py` | Валидация и immutable snapshots runtime-конфигурации |
| `telerixa_core/state.py` | SQLite-схема, checkpoints, outbox, прогресс доставки, heartbeat и архив ошибок |
| `telerixa_core/telegram_reader.py` | Параллельный сбор каналов, альбомы и хронологическое объединение |
| `telerixa_core/media_delivery.py` | Скачивание Telegram-медиа и multipart-доставка в Discord |
| `telerixa_core/rich_messages.py` | Рендеринг native rich-сообщений и извлечение встроенных медиа |
| `telerixa_core/lifecycle.py` | PID-lock, сигналы завершения и мониторинг консоли-владельца |
| `tests/` | Кроссплатформенный набор регрессионных тестов |

## Установка

Windows:

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

На SteamOS/Linux `run.sh` сам создаёт `.venv-linux` и устанавливает зависимости при необходимости.

## Конфигурация

Скопируй пример:

```bat
copy config.example.json config.json
```

Основные настройки:

| Настройка | Назначение |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | Webhook целевого Discord-канала |
| `DISCORD_ALERT_USER_ID` | Необязательный Discord-тег для уведомлений о падении/потере поста |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Данные Telegram-приложения с `my.telegram.org` |
| `TELEGRAM_CHANNELS` | Публичные usernames каналов для авторизованной сессии |
| `LANGUAGE` | `ru` или `en`; позднее можно добавлять новые JSON-каталоги |
| `CHECK_INTERVAL` | Пауза между циклами проверки в секундах |
| `MAX_MESSAGE_LENGTH` | Лимит одного текстового chunk'а Discord |
| `TIMEZONE` | IANA timezone для логов и времени сообщений |
| `DISCORD_FILE_LIMIT_MB` | Лимит одного файла целевого Discord-сервера |
| `LARGE_FILE_ACTION` | `send_text_link`, `skip_post` или `try_send_then_text` |
| `STARTUP_CATCH_UP_LIMIT` | Сколько последних постов нагонять после простоя |
| `MAX_QUEUE_ATTEMPTS` | Лимит несетевых ошибок до переноса сообщения в архив |

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

При открытии `run.sh` или `run_ui.sh` через файловый менеджер SteamOS автоматически запускается видимое окно Konsole.

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

Запуск закреплённой версии Pyright (нужен Node.js):

```bash
python scripts/run_pyright.py
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
