@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 🚀 Запуск Telerixa
echo.
call .venv\Scripts\activate.bat
python telerixa.py
echo.
echo ⛔ Telerixa остановлена
pause
