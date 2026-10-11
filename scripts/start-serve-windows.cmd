@echo off
rem Starts "mcf-apply serve". Double-click it, or point Task Scheduler at it.
rem Keep this window open: closing it stops mcf-apply.
cd /d "%~dp0.."
title mcf-apply serve
".venv\Scripts\mcf-apply.exe" serve
echo.
echo mcf-apply stopped. Press any key to close this window.
pause >nul
