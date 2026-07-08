@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "UI_HOST=127.0.0.1"
set "UI_PORT=8765"
set "UI_URL=http://%UI_HOST%:%UI_PORT%/"

for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%UI_PORT% .*LISTENING"') do (
    set "UI_PID=%%P"
)

if defined UI_PID (
    echo Settings UI is already running at %UI_URL%
    echo PID: %UI_PID%
    echo.
    echo Opening default browser...
    start "" "%UI_URL%"
    echo.
    pause
    exit /b 0
)

echo Starting Telegram -> Discord settings UI
echo.
if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)
python web_ui.py
echo.
echo Settings UI stopped.
pause
