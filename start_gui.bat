@echo off
setlocal
chcp 65001 > nul

cd /d "%~dp0"

net session > nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [INFO] Requesting Administrator permission...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath '%~f0' -Verb RunAs; exit 0 } catch { Write-Host '[ERROR] Administrator permission was not granted.'; exit 1 }"
    if errorlevel 1 (
        echo [FAIL] Administrator permission was not granted.
        pause
        exit /b 1
    )
    exit /b 0
)

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Missing virtual environment: "%PYTHON_EXE%"
    echo Install dependencies into .venv first.
    pause
    exit /b 1
)

echo =====================================================
echo   Stella Sora GUI
echo =====================================================
echo [INFO] Run this from an Administrator shell.
echo [INFO] Use windowed or borderless-windowed mode.
echo [INFO] Game and bot should use the same privilege level.
echo.

"%PYTHON_EXE%" main_gui.py
set "EXIT_CODE=%ERRORLEVEL%"

if %EXIT_CODE% neq 0 (
    echo.
    echo [FAIL] main_gui.py exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
