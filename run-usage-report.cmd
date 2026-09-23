@echo off
setlocal

rem Supports:
rem   run-usage-report.cmd
rem   run-usage-report.cmd 90
rem   run-usage-report.cmd --days 90
rem Additional collector flags such as --scan-all, --codex-home and --output-dir
rem are passed through unchanged.

if "%~1"=="" (
  py "%~dp0collect_codex_usage.py" --days 7
) else if /I "%~1"=="--days" (
  py "%~dp0collect_codex_usage.py" %*
) else if /I "%~1"=="--help" (
  py "%~dp0collect_codex_usage.py" %*
) else if /I "%~1"=="-h" (
  py "%~dp0collect_codex_usage.py" %*
) else (
  py "%~dp0collect_codex_usage.py" --days %*
)

set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo.
  echo Codex usage collection failed with exit code %RESULT%.
  exit /b %RESULT%
)

if not "%CODEX_USAGE_NO_OPEN%"=="1" (
  start "" "%~dp0dashboard\index.html"
)

endlocal
