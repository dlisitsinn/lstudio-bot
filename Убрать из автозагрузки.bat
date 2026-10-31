@echo off
chcp 65001 >nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LStudio-bot.bat" 2>nul
echo Бот больше не запускается сам при включении компьютера.
pause
