@echo off
setlocal

cd /d "%~dp0"

set "APP_NAME=Telerixa"
set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
set "TELERIXA_ALLOW_DETACHED="

echo Starting %APP_NAME%
echo.

if not exist "telerixa.py" (
    echo ERROR: telerixa.py was not found.
    echo Run this file from the Telerixa project folder.
    goto :launcher_error
)

if not exist "config.json" (
    echo ERROR: config.json was not found.
    echo Create it from config.example.json or use run_ui.bat.
    goto :launcher_error
)

if not exist "%PYTHON_EXE%" (
    echo ERROR: The Windows virtual environment was not found.
    echo Create it with: python -m venv .venv
    echo Then install dependencies with: .venv\Scripts\pip install -r requirements.txt
    goto :launcher_error
)

if /I "%~1"=="--check" (
    "%PYTHON_EXE%" -m py_compile "telerixa.py"
    if errorlevel 1 goto :launcher_error
    echo Launcher check passed.
    exit /b 0
)

"%PYTHON_EXE%" "telerixa.py"
set "BOT_EXIT_CODE=%ERRORLEVEL%"

echo.
if "%BOT_EXIT_CODE%"=="0" (
    echo %APP_NAME% stopped normally.
) else (
    echo ERROR: %APP_NAME% stopped with exit code %BOT_EXIT_CODE%.
    echo Check logs\bot.log for details.
)

echo.
pause
exit /b %BOT_EXIT_CODE%

:launcher_error
echo.
echo %APP_NAME% was not started.
echo.
pause
exit /b 1
