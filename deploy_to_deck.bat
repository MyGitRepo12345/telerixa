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

if not defined REMOTE_TMP set "REMOTE_TMP=/home/%DECK_USER%/.telerixa_deploy"
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

for %%F in (telerixa.py i18n.py web_ui.py requirements.txt run.sh run_ui.sh deploy_remote.sh) do (
    if not exist "%%F" (
        echo ERROR: local file is missing: %%F
        exit /b 1
    )
)

for %%F in (locales\en.json locales\ru.json) do (
    if not exist "%%F" (
        echo ERROR: local file is missing: %%F
        exit /b 1
    )
)

for %%F in (telerixa_core\__init__.py telerixa_core\config.py telerixa_core\constants.py telerixa_core\delivery.py telerixa_core\diagnostics.py telerixa_core\discord_delivery.py telerixa_core\formatting.py telerixa_core\lifecycle.py telerixa_core\logging_setup.py telerixa_core\media_delivery.py telerixa_core\models.py telerixa_core\rich_messages.py telerixa_core\state.py telerixa_core\telegram_reader.py) do (
    if not exist "%%F" (
        echo ERROR: local file is missing: %%F
        exit /b 1
    )
)

if not exist "tests\test_*.py" (
    echo ERROR: no local tests were found in tests\.
    exit /b 1
)

set "TEST_PYTHON="
set "TEST_PYTHON_ARGS="

if exist ".venv\Scripts\python.exe" (
    set "TEST_PYTHON=.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "TEST_PYTHON=py"
        set "TEST_PYTHON_ARGS=-3"
    ) else (
        where python >nul 2>nul
        if not errorlevel 1 set "TEST_PYTHON=python"
    )
)

if not defined TEST_PYTHON (
    echo ERROR: Python was not found. Cannot run pre-deploy tests.
    exit /b 1
)

echo Running pre-deploy tests...
"%TEST_PYTHON%" %TEST_PYTHON_ARGS% -W error::ResourceWarning -m unittest discover -s tests -v
if errorlevel 1 (
    echo.
    echo ERROR: Pre-deploy tests failed. Steam Deck was not touched.
    exit /b 1
)

echo Pre-deploy tests passed. Starting deployment...
echo.

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
scp -r telerixa.py i18n.py web_ui.py requirements.txt run.sh run_ui.sh deploy_remote.sh locales telerixa_core %DECK_USER%@%DECK_HOST%:%REMOTE_TMP%/
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
