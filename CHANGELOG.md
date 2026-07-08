# Changelog

All notable changes to Telerixa are documented in this file.

This project follows semantic versioning while it is pre-1.0:

- Patch: bug fixes and small internal improvements.
- Minor: new user-visible features, settings, UI changes, or behavior changes.
- Major: reserved for the first stable `1.0.0` release.

## [0.2.4] - 2026-07-08

### Changed

- Started the modular refactor by moving constants, logging setup, and shared send-result model into `telerixa_core/`.
- Updated Steam Deck deployment to upload, install, back up, and validate the new core package.

## [0.2.3] - 2026-07-08

### Fixed

- Unified Telegram album collection for initial sending, retry delivery, and processed-message state updates.
- Added album collection diagnostics with collected item count and message IDs.

## [0.2.2] - 2026-07-08

### Fixed

- Fixed misleading Steam Deck crash notification output: notification failures are no longer swallowed silently.
- Skipped crash notifications for expected signal exits like deploy stop `143` and Ctrl+C `130`.
- Added explicit Discord webhook response diagnostics for crash notifications.
- Switched Steam Deck crash notifications from `urllib` to `aiohttp`, matching the bot's Discord webhook client.
- Confirmed Discord webhook delivery path with a deliberate test alert.

## [0.2.1] - 2026-07-08

### Fixed

- Fixed Steam Deck deployment after localization by uploading and installing `i18n.py` and `locales/`.
- Added deploy-side validation for required locale files.

## [0.2.0] - 2026-07-08

### Added

- Added JSON-based localization through `locales/en.json` and `locales/ru.json`.
- Added `LANGUAGE` config option.
- Added language selector to the web UI.
- Added reusable `i18n.py` translation helper with English fallback.

### Changed

- Moved bot logs, Discord embed labels, validation messages, and UI text into locale catalogs.
- Replaced Russian code comments and docstrings in Python files with English text.
- Updated README files and config example to document localization.

### Verified

- Verified Python compilation for `telerixa.py`, `web_ui.py`, and `i18n.py`.
- Verified locale JSON parsing.
- Verified web UI rendering in English and Russian.

## [0.1.0] - 2026-07-08

### Added

- Initial Telerixa release.
- Telegram to Discord forwarding via Discord webhooks.
- Support for text, images, videos, albums, reply context, and forward context.
- SQLite state storage for channel checkpoints, processed messages, and retry queue.
- Runtime configuration through `config.json`.
- Web UI for local configuration changes.
- Hot config reload without restarting the bot.
- Configurable Discord file-size limit and large-file behavior.
- Retry queue with terminal failure handling for non-retryable cases.
- Steam Deck deployment flow over SSH.
- Console and rotating file logs.
