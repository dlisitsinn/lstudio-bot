@echo off
chcp 65001 >nul
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
(
echo @echo off
echo chcp 65001 ^>nul
echo set PYTHONUTF8=1
echo cd /d "%~dp0"
echo start "LStudio bot" /min cmd /c "python updater.py & python main.py --no-browser"
) > "%STARTUP%\LStudio-bot.bat"
echo.
echo Готово: бот будет сам запускаться при включении компьютера (свёрнутым окном).
echo Окно управления: http://127.0.0.1:8765 - добавьте его в закладки браузера.
echo Убрать из автозагрузки - файл «Убрать из автозагрузки.bat».
echo.
pause
