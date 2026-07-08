# Telerixa

Reliable Telegram-to-Discord forwarding for media-heavy news channels.

Telerixa is a production-style Telegram to Discord forwarding bot built for a real daily workflow: monitoring multiple Telegram news channels and reposting text, photos, videos, albums, and reply context into Discord through webhooks.

Current version: `0.2.4`

This repository is also a QA portfolio project: it contains not only the bot itself, but also reliability work around retries, state persistence, file-size limits, runtime configuration, logging, and Steam Deck deployment.

[Russian README](README_RU.md)

## Features

- Forwards posts from multiple Telegram channels to Discord.
- Supports text, images, videos, albums, and Telegram reply/forward context.
- Uses Discord webhooks, so no Discord bot token is required.
- Runtime configuration via `config.json` and a local web UI.
- Hot config reload without restarting the bot.
- SQLite state storage for channel checkpoints, sent messages, and retry queue.
- Retry queue for temporary network or Discord failures.
- Configurable Discord file-size limit and behavior for oversized media.
- Startup catch-up limit for recovering after downtime without reposting the full backlog.
- Console and file logging.
- JSON-based localization with English and Russian catalogs.
- Optional Discord alert mention when the bot crashes.
- SSH deployment flow for Steam Deck without overwriting runtime files.

## Reliability Notes

The bot is designed around failure cases that appeared during real use:

- Internet connection drops.
- Telegram download interruptions.
- Discord webhook errors.
- Discord file-size limit changes after server boost changes.
- Duplicate posts after restart.
- Albums where the caption is attached to a non-first media item.
- Telegram replies/forwards that need extra context in Discord.

Runtime state is stored in SQLite, so the bot can restart without losing its queue or channel checkpoints.

## Tech Stack

- Python
- Telethon
- aiohttp
- SQLite
- Lightweight built-in web UI
- Windows batch scripts
- SteamOS/Linux shell scripts

## Setup

Install dependencies:

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

On SteamOS/Linux, `run.sh` creates and uses a Linux virtual environment automatically when needed.

## Configuration

Copy the example config:

```bat
copy config.example.json config.json
```

Fill in:

- `DISCORD_WEBHOOK_URL`
- `DISCORD_ALERT_USER_ID`, optional crash mention target
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_CHANNELS`
- `LANGUAGE`, for example `en` or `ru`
- `DISCORD_FILE_LIMIT_MB`
- `LARGE_FILE_ACTION`
- `STARTUP_CATCH_UP_LIMIT`
- `MAX_QUEUE_ATTEMPTS`

Never commit the real `config.json`.

## Running

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

or on SteamOS/Linux:

```bash
./run_ui.sh
```

## Steam Deck Deployment

Create a local deploy config:

```bat
copy deploy_config.example.bat deploy_config.local.bat
```

Edit `deploy_config.local.bat`, then run:

```bat
deploy_to_deck.bat
```

The deploy script uploads code files only. Runtime files on the Steam Deck are preserved:

- `config.json`
- `bot_state.db`
- `tg_session.session`
- `logs/`

## Git Safety

See [GIT_SETUP.md](GIT_SETUP.md) before the first commit.

Ignored local/runtime files include:

- `config.json`
- `deploy_config.local.bat`
- `bot_state.db`
- `tg_session.session`
- `seen_messages.json`
- `logs/`
- `.venv/`
- `.venv-linux/`

## QA-Relevant Highlights

This project is useful for QA interviews because it demonstrates:

- Real bug investigation through logs and reproducible edge cases.
- Defensive handling of network and API failures.
- Retry and dead-letter decisions for failed messages.
- Runtime configuration validation.
- Regression risk around file limits, albums, forwards, replies, and restarts.
- Cross-platform behavior across Windows and SteamOS/Linux.

## Links

- Telethon docs: https://docs.telethon.dev/
- Discord webhooks: https://discord.com/developers/docs/resources/webhook
- Telegram API: https://my.telegram.org/
