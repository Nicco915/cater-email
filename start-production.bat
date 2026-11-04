@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title CATER - PRODUCTION
echo CATER PRODUCTION
echo Data: %CD%\runtime\production
echo Web: http://127.0.0.1:8765/
echo Close this window or press Ctrl+C to stop.
echo.
if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found. Run scripts\install.ps1 first.
    pause
    exit /b 1
)
if not exist "config\production.local.toml" (
    copy /y "config\production.example.toml" "config\production.local.toml" >nul
    if errorlevel 1 (
        echo Failed to create local configuration.
        pause
        exit /b 1
    )
    echo Created config\production.local.toml.
    echo Edit the allowlist before sending test or production mail.
)
if not exist ".env" (
    echo No .env found. Mail uses OS environment or TOML settings.
)
echo Sending is controlled by this environment's effective settings.
echo Test mode can send real mail when explicitly enabled.
echo Do not run both modes against the same mailbox folder.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-web.ps1" -Config "config\production.local.toml" -Runtime "runtime\production" -Port 8765
set "CATER_EXIT_CODE=%ERRORLEVEL%"
if not "%CATER_EXIT_CODE%"=="0" (
    echo CATER exited with code %CATER_EXIT_CODE%.
    pause
)
exit /b %CATER_EXIT_CODE%
