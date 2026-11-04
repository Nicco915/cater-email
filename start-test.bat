@echo off
chcp 65001 >nul
if /I "%~1"=="--console" goto run
start "CATER TEST" "%ComSpec%" /D /K call "%~f0" --console
exit /b
:run
setlocal
cd /d "%~dp0"
title CATER - TEST
echo CATER TEST
echo Data: %CD%\runtime\test
echo Web: http://127.0.0.1:8766/
echo Keep this window open. Errors remain visible here.
echo.
if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found. Double-click initialize-test.bat first.
    exit /b 1
)
if not exist "config\test.local.toml" (
    copy /y "config\test.example.toml" "config\test.local.toml" >nul
    if errorlevel 1 exit /b 1
    echo Created config\test.local.toml. Review the allowlist.
)
echo Sending follows the effective settings of this environment.
echo Do not run test and production against the same mailbox folder.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-web.ps1" -Config "config\test.local.toml" -Runtime "runtime\test" -Port 8766 -OpenBrowser
set "CATER_EXIT_CODE=%ERRORLEVEL%"
echo.
echo CATER stopped. Exit code: %CATER_EXIT_CODE%.
echo Review the error above and runtime logs before restarting.
exit /b %CATER_EXIT_CODE%
