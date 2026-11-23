@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo Media Downloader Studio を起動しています...
python app.py
pause
