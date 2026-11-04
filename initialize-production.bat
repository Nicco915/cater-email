@echo off
chcp 65001 >nul
if /I "%~1"=="--console" goto run
start "CATER PRODUCTION INITIALIZATION" "%ComSpec%" /D /K call "%~f0" --console
exit /b
:run
setlocal
cd /d "%~dp0"
title CATER - PRODUCTION INITIALIZATION
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\initialize-production.ps1"
set "CATER_EXIT_CODE=%ERRORLEVEL%"
echo.
echo Initialization stopped. Exit code: %CATER_EXIT_CODE%.
echo Keep this window for diagnostics. Close it when finished.
exit /b %CATER_EXIT_CODE%
