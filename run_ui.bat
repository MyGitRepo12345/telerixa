@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "UI_HOST=127.0.0.1"
set "UI_PORT=8765"
set "UI_URL=http://%UI_HOST%:%UI_PORT%/"
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
set "TELERIXA_ALLOW_DETACHED="

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
if not exist "%PYTHON_EXE%" (
    echo ERROR: The Windows virtual environment was not found.
    echo Create it with: python -m venv .venv
    echo Then install dependencies with: .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
"%PYTHON_EXE%" "web_ui.py"
set "UI_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Settings UI stopped with exit code %UI_EXIT_CODE%.
pause
exit /b %UI_EXIT_CODE%
