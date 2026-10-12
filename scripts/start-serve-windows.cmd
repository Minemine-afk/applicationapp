@echo off
rem Runs "mcf-apply serve" and restarts it if it stops. `mcf-apply setup` makes this
rem start (minimised) when you log in. Close this window to stop mcf-apply.
cd /d "%~dp0.."
title mcf-apply
:loop
rem Reinstall the app from the checkout while nothing is running, so /update (which only
rem pulls the code) and a broken install both get fixed here before each start.
".venv\Scripts\python.exe" -m pip install -q -e . >nul 2>&1
if errorlevel 1 (
    echo Reinstall failed; starting the current version anyway.
    ".venv\Scripts\python.exe" -m pip install -q -e .
)
".venv\Scripts\mcf-apply.exe" serve
rem Exit code 3: another copy is already running, so this one is not needed.
if %errorlevel%==3 exit /b 0
echo.
echo mcf-apply stopped. Restarting in 20 seconds (close this window to stop it)...
timeout /t 20 /nobreak >nul
goto loop
