@echo off
rem Startet das Kamera-Update-Tool und oeffnet den Browser.
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py
start "" cmd /c "timeout /t 2 >nul & start http://localhost:8080"
%PY% server.py %*
pause
