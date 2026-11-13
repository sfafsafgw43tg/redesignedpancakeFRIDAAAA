@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>nul
title AntiDetect Android Emulator

echo ==================================================================
echo   AntiDetect Android Emulator - one-click start
echo ==================================================================
echo.

rem ---------------------------------------------------------------
rem 1. Find a Python interpreter: local .venv, then the py launcher,
rem    then plain python. ("py" needs its "-3" switch separately so that
rem    paths containing spaces keep working.)
rem ---------------------------------------------------------------
set "PY="
set "PY_ARGS="

if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY=%~dp0.venv\Scripts\python.exe"
    echo [env] Using virtual environment: .venv
    goto :have_python
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PY=py"
        set "PY_ARGS=-3"
        goto :have_python
    )
)

where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PY=python"
        goto :have_python
    )
)

echo [ERROR] Python 3 was not found on this computer.
echo.
echo   1. Install Python 3.10 or newer from https://www.python.org/downloads/
echo      (tick "Add python.exe to PATH" in the installer)
echo   2. Close this window, then double-click START.bat again.
echo.
pause
exit /b 1

:have_python
echo [env] Interpreter: %PY% %PY_ARGS%
"%PY%" %PY_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.8 or newer is required.
    "%PY%" %PY_ARGS% --version
    echo.
    pause
    exit /b 1
)
echo.

rem ---------------------------------------------------------------
rem 2. Hand over to bootstrap.py, which:
rem      - installs the Python dependencies (plus pywebview)
rem      - asks you to pick the Android SDK folder
rem      - installs platform-tools, emulator and a system image
rem      - opens the app in a native desktop window
rem
rem    Extra options are passed straight through, e.g.:
rem        START.bat --android 13
rem        START.bat --sdk "C:\Android\Sdk"
rem        START.bat --browser
rem ---------------------------------------------------------------
"%PY%" %PY_ARGS% bootstrap.py %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
    echo.
    echo [INFO] bootstrap exited with code %EXITCODE%.
    echo        Emulator launch logs are in data\logs\.
    echo.
    pause
)

endlocal & exit /b %EXITCODE%
