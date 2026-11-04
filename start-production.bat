@echo off
chcp 65001 >nul
if /I "%~1"=="--console" goto run
start "CATER PRODUCTION" "%ComSpec%" /D /K call "%~f0" --console
exit /b
:run
setlocal
cd /d "%~dp0"
title CATER - PRODUCTION
echo CATER PRODUCTION
echo Data: %CD%\runtime\production
echo Web: http://127.0.0.1:8765/
echo Keep this window open. Errors remain visible here.
echo.
if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found. Double-click initialize-test.bat first.
    exit /b 1
)
if not exist "config\production.local.toml" (
    copy /y "config\production.example.toml" "config\production.local.toml" >nul
    if errorlevel 1 exit /b 1
    echo Created config\production.local.toml. Review the allowlist.
)
echo Sending follows the effective settings of this environment.
echo Do not run test and production against the same mailbox folder.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-web.ps1" -Config "config\production.local.toml" -Runtime "runtime\production" -Port 8765 -OpenBrowser
set "CATER_EXIT_CODE=%ERRORLEVEL%"
echo.
echo CATER stopped. Exit code: %CATER_EXIT_CODE%.
echo Review the error above and runtime logs before restarting.
exit /b %CATER_EXIT_CODE%
