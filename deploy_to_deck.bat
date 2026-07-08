@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

if exist "deploy_config.local.bat" (
    call "deploy_config.local.bat"
) else (
    echo ERROR: deploy_config.local.bat was not found.
    echo Copy deploy_config.example.bat to deploy_config.local.bat and edit it.
    exit /b 1
)

if not defined DECK_USER (
    echo ERROR: DECK_USER is empty in deploy_config.local.bat.
    exit /b 1
)

if not defined DECK_HOST (
    echo ERROR: DECK_HOST is empty in deploy_config.local.bat.
    exit /b 1
)

if not defined REMOTE_DIR (
    echo ERROR: REMOTE_DIR is empty in deploy_config.local.bat.
    exit /b 1
)

if not defined REMOTE_TMP set "REMOTE_TMP=/home/%DECK_USER%/.tg_forwarder_deploy"
if not defined START_BOT set "START_BOT=1"

echo Deploy Telerixa to Steam Deck
echo Target: %DECK_USER%@%DECK_HOST%:"%REMOTE_DIR%"
echo.

where ssh >nul 2>nul
if errorlevel 1 (
    echo ERROR: ssh was not found in PATH.
    exit /b 1
)

where scp >nul 2>nul
if errorlevel 1 (
    echo ERROR: scp was not found in PATH.
    exit /b 1
)

for %%F in (Script.py web_ui.py requirements.txt run.sh run_ui.sh deploy_remote.sh) do (
    if not exist "%%F" (
        echo ERROR: local file is missing: %%F
        exit /b 1
    )
)

echo Validating remote folder...
ssh %DECK_USER%@%DECK_HOST% "test -d '%REMOTE_DIR%' && test -f '%REMOTE_DIR%/config.json'"
if errorlevel 1 (
    echo.
    echo ERROR: Remote folder validation failed.
    echo Expected existing folder with config.json:
    echo   %REMOTE_DIR%
    echo Refusing to deploy.
    exit /b 1
)

echo Preparing remote staging folder...
ssh %DECK_USER%@%DECK_HOST% "rm -rf '%REMOTE_TMP%' && mkdir -p '%REMOTE_TMP%'"
if errorlevel 1 (
    echo ERROR: Could not prepare remote staging folder.
    exit /b 1
)

echo Uploading code files...
scp Script.py web_ui.py requirements.txt run.sh run_ui.sh deploy_remote.sh %DECK_USER%@%DECK_HOST%:%REMOTE_TMP%/
if errorlevel 1 (
    echo ERROR: Upload failed.
    exit /b 1
)

echo Installing on Steam Deck...
ssh %DECK_USER%@%DECK_HOST% "sh '%REMOTE_TMP%/deploy_remote.sh' '%REMOTE_DIR%' '%REMOTE_TMP%' '%START_BOT%'"
if errorlevel 1 (
    echo ERROR: Remote install failed.
    exit /b 1
)

echo.
echo Deploy finished.
echo Bot autostart: %START_BOT%
echo A visible Konsole window should open on Steam Deck.
echo If it did not open, start the bot manually on Steam Deck:
echo   cd "%REMOTE_DIR%"
echo   ./run.sh
echo Konsole launch debug log:
echo   cat "%REMOTE_DIR%/logs/konsole_start.log"
echo Bot file log:
echo   tail -n 80 "%REMOTE_DIR%/logs/bot.log"
echo.
pause
