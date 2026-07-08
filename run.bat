@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 🚀 Запуск форварда Telegram -> Discord
echo.
call .venv\Scripts\activate.bat
python Script.py
echo.
echo ⛔ Форвард остановлен
pause
