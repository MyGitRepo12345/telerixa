# Changelog

All notable changes to Telerixa are documented in this file.

This project follows semantic versioning while it is pre-1.0:

- Patch: bug fixes and small internal improvements.
- Minor: new user-visible features, settings, UI changes, or behavior changes.
- Major: reserved for the first stable `1.0.0` release.

## [0.3.0] - 2026-07-15

### Added

- Added concurrent Telegram channel collection with deterministic global chronological delivery.
- Added immutable runtime configuration snapshots and atomic hot reloads.
- Added durable delivery progress for restart-safe media and text resume.
- Added source timestamps to queued messages so retries preserve Telegram chronology.
- Added regression coverage for configuration, state, formatting, Discord delivery, media resume, Telegram collection, and the settings UI.
- Added a mandatory local test gate before Steam Deck deployment starts.

### Changed

- Split the former monolithic bot into focused modules for configuration, state, delivery, Discord, media, Telegram reading, formatting, logging, constants, and shared models.
- Changed channel polling to collect concurrently while keeping Discord delivery sequential and ordered.
- Changed retry processing to continue best effort when an individual post cannot be delivered.
- Updated Steam Deck deployment validation for the modular package layout.
- Long reply and forward context is now split across Discord messages instead of being truncated.

### Fixed

- Fixed SQLite connections remaining open after state operations.
- Fixed transient network failures consuming the bounded terminal retry budget.
- Fixed permanently unavailable queued posts retrying forever instead of becoming explicit failed records.
- Fixed partial Discord deliveries restarting from the beginning after a retry or process restart.
- Fixed generic Telegram connection failures being treated like invalid session errors.
- Fixed fatal startup failures returning a successful process exit code or silently losing crash-alert failures.
- Fixed inconsistent Telegram album discovery between initial delivery, retries, and stored message statuses.

## [0.2.7] - 2026-07-08

### Added

- Added regression tests for SQLite state storage helpers.
- Added regression tests for Telegram message formatting helpers.
- Added `requirements-dev.txt` for local test tooling.

## [0.2.6] - 2026-07-08

### Fixed

- Switched the local settings UI to a single-threaded HTTP server to prevent language state races during page rendering.

### Changed

- Moved SQLite state storage helpers into `telerixa_core/state.py`.
- Kept state storage independent from runtime globals by passing database paths and prepared timestamps explicitly.
- Updated Steam Deck deployment validation for the new state module.

## [0.2.5] - 2026-07-08

### Changed

- Moved Telegram post text formatting helpers into `telerixa_core/formatting.py`.
- Kept reply and cross-reply formatting behavior by passing the Telegram client explicitly.
- Updated Steam Deck deployment validation for the new formatting module.

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
