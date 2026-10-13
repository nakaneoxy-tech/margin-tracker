@echo off
cd /d "%~dp0"
start "" http://localhost:8790
python server.py
