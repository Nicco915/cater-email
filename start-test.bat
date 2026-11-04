@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title CATER - TEST
echo CATER TEST
echo Data: %CD%\runtime\test
echo Web: http://127.0.0.1:8766/
echo Close this window or press Ctrl+C to stop.
echo.
if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found. Run scripts\install.ps1 first.
    pause
    exit /b 1
)
if not exist "config\test.local.toml" (
    copy /y "config\test.example.toml" "config\test.local.toml" >nul
    if errorlevel 1 (
        echo Failed to create local configuration.
        pause
        exit /b 1
    )
    echo Created config\test.local.toml.
    echo Edit the allowlist before sending test or production mail.
)
if not exist ".env" (
    echo No .env found. Mail uses OS environment or TOML settings.
)
echo Sending is controlled by this environment's effective settings.
echo Test mode can send real mail when explicitly enabled.
echo Do not run both modes against the same mailbox folder.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-web.ps1" -Config "config\test.local.toml" -Runtime "runtime\test" -Port 8766
set "CATER_EXIT_CODE=%ERRORLEVEL%"
if not "%CATER_EXIT_CODE%"=="0" (
    echo CATER exited with code %CATER_EXIT_CODE%.
    pause
)
exit /b %CATER_EXIT_CODE%
