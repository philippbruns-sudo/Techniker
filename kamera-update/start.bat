@echo off
rem Starts the camera update tool and opens the browser.
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py
start "" cmd /c "timeout /t 2 >nul & start http://localhost:8080"
%PY% server.py %*
pause
