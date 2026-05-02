@echo off
title DailyBot - Running
:loop
echo Starting DailyBot...
..\venv\Scripts\python.exe main.py
echo Crashed or stopped. Restarting in 10s...
timeout /t 10
goto loop
