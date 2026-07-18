# Telerixa

[![CI](https://github.com/MyGitRepo12345/telerixa/actions/workflows/ci.yml/badge.svg)](https://github.com/MyGitRepo12345/telerixa/actions/workflows/ci.yml)

Reliable Telegram-to-Discord forwarding for media-heavy news channels.

Telerixa is a production-style Telegram to Discord forwarding bot built for a real daily workflow: monitoring multiple Telegram news channels and reposting text, photos, videos, albums, native rich messages, and reply context into Discord through webhooks.

Current version: `0.5.1`

This repository is also a QA portfolio project: it contains not only the bot itself, but also reliability work around retries, state persistence, file-size limits, runtime configuration, logging, and Steam Deck deployment.

[Russian README](README_RU.md)

## Features

- Forwards posts from multiple Telegram channels to Discord.
- Polls Telegram channels concurrently, then delivers collected posts in global chronological order.
- Supports text, images, videos, albums, and complete Telegram reply/forward context.
- Converts native Telegram rich messages into Discord-compatible headings, inline styles, lists, checklists, quotes, code, tables, formulas, details, maps, and ordered media attachments.
- Preserves Telegram text spoilers using Discord spoiler markup, including replies and media captions.
- Uses Discord webhooks, so no Discord bot token is required.
- Runtime configuration via `config.json` and a local web UI.
- Atomic hot config reload without restarting the bot.
- SQLite state storage for channel checkpoints, sent messages, and retry queue.
- Durable retry queue with partial-delivery progress and restart-safe resume.
- Persistent failed-delivery archive with manual requeue, dismissal, and Telegram source links.
- Clear pending feedback for manual retries, with duplicate requests disabled until the bot processes them.
- Separate transient and terminal failure handling so one bad post does not block the feed.
- Live operational dashboard with heartbeat, queue state, channel checkpoints, and failure actions.
- On-demand diagnostics for SQLite, Discord webhook access, Telegram session state, storage, and FFmpeg.
- Configurable Discord file-size limit and behavior for oversized media.
- Optional asynchronous FFmpeg video compression to fit oversized videos under the configured Discord limit, with text/link fallback when conversion is unavailable.
- Startup catch-up limit for recovering after downtime without reposting the full backlog.
- Color-coded console and web UI logs with plain-text rotating log files.
- JSON-based localization with English and Russian catalogs.
- Optional Discord alert mention when the bot crashes.
- Single-instance protection for the bot and settings UI.
- Console-bound process lifetime with graceful signal handling and PID-file cleanup.
- Test-gated SSH deployment for Steam Deck without overwriting runtime files.

## How It Works

1. Telegram channels are polled concurrently through one authorized Telethon session.
2. New posts are normalized into one global chronological batch, with albums deduplicated as a single delivery unit.
3. Text, reply context, spoilers, rich-message blocks, and media are converted into Discord-compatible payloads.
4. Discord delivery remains sequential to preserve ordering. Confirmed progress is stored after every media upload or text chunk.
5. Retryable failures enter the SQLite outbox; permanent or exhausted failures move into the persistent archive instead of blocking later posts.

## Web UI

The local UI listens on `127.0.0.1:8765` by default and provides:

- an Overview with process health, heartbeat, delivery metrics, channel checkpoints, retry queue, and failure archive;
- runtime-safe settings with validation and atomic hot reload;
- manual retry, requeue, dismiss, and queue-clear actions;
- system diagnostics and color-coded live logs without exposing the webhook in Overview responses.

Diagnostics verify `ffmpeg`, `ffprobe`, and the required H.264/AAC encoders. When `compress_then_text` is selected, Telerixa prefers existing system tools and otherwise downloads a pinned, SHA-256-verified user-local FFmpeg build. Overview also shows the active conversion and the most recent result.

## Reliability Notes

The bot is designed around failure cases that appeared during real use:

- Internet connection drops.
- Telegram download interruptions.
- Discord webhook errors.
- Discord file-size limit changes after server boost changes.
- Oversized videos that need conversion without blocking heartbeat or channel polling.
- Duplicate posts after restart.
- Albums where the caption is attached to a non-first media item.
- Telegram replies/forwards that need extra context in Discord.
- Long reply/forward context that must be split without truncation.
- Rich-only Telegram posts whose ordinary text and media fields are empty.

Runtime state is stored in SQLite, so the bot can restart without losing its queue or channel checkpoints.

The Windows and SteamOS launchers keep Telerixa attached to their owning console. Closing that console stops the corresponding bot or UI process, while PID locks prevent accidental duplicate instances and stale port ownership.

## Tech Stack

- Python
- Telethon
- aiohttp
- SQLite
- Lightweight built-in web UI
- Windows batch scripts
- SteamOS/Linux shell scripts

## Project Structure

| Path | Responsibility |
| --- | --- |
| `telerixa.py` | Application orchestration, Telegram session lifecycle, polling, and queue processing |
| `web_ui.py` | Local configuration and operational dashboard |
| `telerixa_core/config.py` | Immutable runtime configuration snapshots and validation |
| `telerixa_core/state.py` | SQLite schema, checkpoints, outbox, delivery progress, heartbeat, and failure archive |
| `telerixa_core/telegram_reader.py` | Concurrent channel collection, album discovery, and chronological merging |
| `telerixa_core/media_delivery.py` | Telegram media downloads and Discord multipart delivery |
| `telerixa_core/transcoding.py` | Async FFmpeg probing, bitrate planning, bounded conversion, and cleanup |
| `telerixa_core/ffmpeg_tools.py` | System-first discovery and verified managed FFmpeg bootstrap |
| `telerixa_core/rich_messages.py` | Native Telegram rich-message rendering and embedded-media extraction |
| `telerixa_core/lifecycle.py` | PID locks, signal handling, and owner-console monitoring |
| `tests/` | Cross-platform regression suite |

## Setup

Windows:

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On SteamOS/Linux, `run.sh` creates `.venv-linux` and installs dependencies automatically when needed.

## Configuration

Copy the example config:

```bat
copy config.example.json config.json
```

Important settings:

| Setting | Purpose |
| --- | --- |
| `DISCORD_WEBHOOK_URL` | Destination Discord webhook |
| `DISCORD_ALERT_USER_ID` | Optional Discord user mention for crash/drop alerts |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | Telegram application credentials from `my.telegram.org` |
| `TELEGRAM_CHANNELS` | Public channel usernames monitored by the authorized session |
| `LANGUAGE` | `en` or `ru`; additional JSON catalogs can be added later |
| `CHECK_INTERVAL` | Seconds between polling cycles |
| `MAX_MESSAGE_LENGTH` | Discord text chunk limit |
| `TIMEZONE` | IANA timezone used in logs and message timestamps |
| `DISCORD_FILE_LIMIT_MB` | Per-file upload limit for the destination server |
| `LARGE_FILE_ACTION` | `compress_then_text`, `send_text_link`, `skip_post`, or `try_send_then_text` |
| `VIDEO_TRANSCODE_PRESET` | `fast`, `balanced`, or `quality` FFmpeg preset |
| `VIDEO_TRANSCODE_TIMEOUT_SECONDS` | Total time budget for one video conversion, from 30 to 7200 seconds |
| `STARTUP_CATCH_UP_LIMIT` | Number of recent posts considered after downtime |
| `MAX_QUEUE_ATTEMPTS` | Retry budget for non-network failures |

Never commit the real `config.json`.

Video conversion uses system `ffmpeg` and `ffprobe` when both are available. Otherwise, Telerixa's standard-library bootstrap downloads fixed FFmpeg 8.1.2 archives from the retained [BtbN FFmpeg Builds release](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-06-30-13-34). The versioned release URL never points to `latest`, and both the exact byte size and pinned SHA-256 digest must match before extraction. Only `ffmpeg` and `ffprobe` are extracted into `.telerixa-tools/`; no third-party Python installer or `sudo` is involved. A failed download can retry after a cooldown without restarting the bot. If setup or conversion fails, Telerixa sends the post text and Telegram link instead of leaving the post in an endless retry loop.

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

Opening `run.sh` or `run_ui.sh` from the SteamOS file manager also launches a visible Konsole window.

Web UI:

```bat
run_ui.bat
```

or on SteamOS/Linux:

```bash
./run_ui.sh
```

## Testing

Install development dependencies when you want to run regression tests.

Windows:

```bat
.venv\Scripts\pip install -r requirements-dev.txt
```

SteamOS/Linux:

```bash
.venv-linux/bin/pip install -r requirements-dev.txt
```

Run tests:

```bash
python -m pytest
```

The test suite also works without pytest:

```bash
python -W error::ResourceWarning -m unittest discover -s tests -v
```

Run the pinned Pyright version (requires Node.js):

```bash
python scripts/run_pyright.py
```

GitHub Actions runs the full regression suite and Pyright on Windows and Ubuntu for every push and pull request.

## Steam Deck Deployment

Create a local deploy config:

```bat
copy deploy_config.example.bat deploy_config.local.bat
```

Edit `deploy_config.local.bat`, then run:

```bat
deploy_to_deck.bat
```

The deploy script runs the complete local test suite first. If any test fails, deployment stops before the first SSH connection and the Steam Deck is not modified.

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
