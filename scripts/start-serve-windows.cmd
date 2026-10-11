@echo off
rem Runs "mcf-apply serve" and restarts it if it stops. `mcf-apply setup` makes this
rem start (minimised) when you log in. Close this window to stop mcf-apply.
cd /d "%~dp0.."
title mcf-apply
:loop
".venv\Scripts\mcf-apply.exe" serve
rem Exit code 3: another copy is already running, so this one is not needed.
if %errorlevel%==3 exit /b 0
echo.
echo mcf-apply stopped. Restarting in 60 seconds (close this window to stop it)...
timeout /t 60 /nobreak >nul
goto loop
