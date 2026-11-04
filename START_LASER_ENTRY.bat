@echo off
title Laser Entry
cd /d "%~dp0"

where py >nul 2>nul && (set PY=py) || (set PY=python)

echo Installing / checking Flask...
%PY% -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Could not install Flask. Check internet connection and that Python is installed.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  Laser Entry is starting.
echo  On THIS PC open:      http://localhost:5000
echo  On phones / other PCs (same Wi-Fi/network) open one of:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do for /f "tokens=*" %%b in ("%%a") do echo        http://%%b:5000
echo.
echo  First login:  admin / admin123   (change it after login)
echo  Keep this window OPEN. Closing it stops the system.
echo ============================================================
echo.
start "" http://localhost:5000
%PY% app.py
pause
