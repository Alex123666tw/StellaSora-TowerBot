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
echo   Stella Sora Elevated Test Runner
echo =====================================================
echo [INFO] This runner accepts only whitelisted test requests.
echo [INFO] Request tests from a normal shell with:
echo [INFO]   .\.venv\Scripts\python.exe diagnostics\request_elevated_test.py run-safe-single-round
echo [INFO] Stop runner with:
echo [INFO]   .\.venv\Scripts\python.exe diagnostics\request_elevated_test.py stop
echo.

"%PYTHON_EXE%" diagnostics\elevated_test_runner.py --idle-timeout 0
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo [INFO] elevated_test_runner.py exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
