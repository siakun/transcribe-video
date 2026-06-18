@echo off
REM Front-door launcher: builds if sources changed, otherwise runs the last build.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %errorlevel%
