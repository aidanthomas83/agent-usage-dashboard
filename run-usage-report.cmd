@echo off
setlocal

set "DAYS=%~1"
if "%DAYS%"=="" set "DAYS=7"

py "%~dp0collect_codex_usage.py" --days %DAYS%
if errorlevel 1 (
  echo.
  echo Codex usage collection failed.
  exit /b %errorlevel%
)

start "" "%~dp0dashboard\index.html"
endlocal
