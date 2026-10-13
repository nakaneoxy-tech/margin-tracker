@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo [%date% %time%] >> data\update.log
python tracker.py >> data\update.log 2>&1
