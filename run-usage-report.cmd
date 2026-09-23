@echo off
setlocal

rem Supports both:
rem   run-usage-report.cmd 90
rem   run-usage-report.cmd --days 90
rem Additional collector flags such as --scan-all are passed through.

if "%~1"=="" (
  set "ARGS=--days 7"
) else (
  set "FIRST=%~1"
  if "%FIRST:~0,1%"=="-" (
    set "ARGS=%*"
  ) else (
    set "ARGS=--days %*"
  )
)

py "%~dp0collect_codex_usage.py" %ARGS%
if errorlevel 1 (
  echo.
  echo Codex usage collection failed.
  exit /b %errorlevel%
)

start "" "%~dp0dashboard\index.html"
endlocal
