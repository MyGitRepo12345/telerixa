# Git checklist

## What is safe to commit

- `Script.py`
- `web_ui.py`
- `requirements.txt`
- `run.bat`, `run.sh`, `run_ui.bat`, `run_ui.sh`
- `deploy_to_deck.bat`, `deploy_remote.sh`
- `config.example.json`
- `deploy_config.example.bat`
- `.gitignore`, `.gitattributes`
- docs and README files

## What must stay local

These files are ignored by Git:

- `config.json`
- `deploy_config.local.bat`
- `bot_state.db`, `bot_state.db-*`
- `tg_session.session`, `tg_session.session-*`
- `seen_messages.json`
- `logs/`
- `.venv/`, `.venv-linux/`

## First-time setup

```bash
git init
git status --short
git add .
git status --short
git commit -m "Initial Telerixa release"
```

Before pushing, check that `git status --short` does not show real config, session, database, logs, or virtualenv files.

## Deploy config

Deployment uses local settings from `deploy_config.local.bat`.

For a new machine:

```bat
copy deploy_config.example.bat deploy_config.local.bat
notepad deploy_config.local.bat
```

Then set the Steam Deck host, remote folder, and autostart flag there.
