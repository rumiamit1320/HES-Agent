@echo off
setlocal EnableExtensions
title HES Power Outage Agent - One Click Launch

REM Always resolve paths relative to this launcher.
cd /d "%~dp0"
set "ROOT=%~dp0"
set "EXE=%ROOT%HES_Power_Outage_Agent.exe"

echo ============================================================
echo HES POWER OUTAGE AGENT
echo One-click setup and launch
echo ============================================================
echo.

REM ------------------------------------------------------------
REM EXE distribution mode
REM The Windows EXE already contains the Python application and
REM dependencies. Do not create/use a Python venv in this mode.
REM ------------------------------------------------------------
if exist "%EXE%" (
    echo Windows EXE detected.
    echo Launching:
    echo %EXE%
    echo.
    "%EXE%"
    set "EXITCODE=%errorlevel%"
    echo.
    echo ============================================================
    echo HES Power Outage Agent exited with code %EXITCODE%
    echo ============================================================
    pause
    exit /b %EXITCODE%
)

REM ------------------------------------------------------------
REM Source distribution fallback
REM ------------------------------------------------------------
set "REQ=%ROOT%requirements.txt"
set "VENV=%ROOT%.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"

if not exist "%REQ%" (
    echo ERROR: requirements.txt was not found.
    echo Expected:
    echo %REQ%
    echo.
    echo The Windows package is incomplete.
    pause
    exit /b 1
)

set "PYTHON_CMD="
where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>&1
    if %errorlevel%==0 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo Python was not found.
    echo Attempting automatic installation with Windows winget...
    where winget >nul 2>&1
    if %errorlevel% neq 0 (
        echo ERROR: winget is not available.
        echo Please install Python 3.12+ manually.
        pause
        exit /b 1
    )
    winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
    if %errorlevel% neq 0 (
        echo ERROR: Python installation failed.
        pause
        exit /b 1
    )
    set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%PATH%"
    where py >nul 2>&1
    if %errorlevel%==0 (
        set "PYTHON_CMD=py -3"
    ) else (
        set "PYTHON_CMD=python"
    )
)

echo Python: %PYTHON_CMD%
echo.

if not exist "%VENV_PY%" (
    echo Creating isolated Python environment...
    %PYTHON_CMD% -m venv "%VENV%"
    if %errorlevel% neq 0 (
        echo ERROR: Could not create the virtual environment.
        pause
        exit /b 1
    )
)

echo Upgrading pip...
"%VENV_PY%" -m pip install --upgrade pip
if %errorlevel% neq 0 (
    echo ERROR: pip setup failed.
    pause
    exit /b 1
)

echo Installing project dependencies...
"%VENV_PY%" -m pip install -r "%REQ%"
if %errorlevel% neq 0 (
    echo ERROR: Dependency installation failed.
    pause
    exit /b 1
)

echo Installing Playwright browser support...
"%VENV_PY%" -m playwright install chromium
if %errorlevel% neq 0 (
    echo WARNING: Playwright Chromium installation failed.
)

echo.
echo Starting HES Power Outage Agent from source...
echo.
"%VENV_PY%" -m hes_agent.agent
set "EXITCODE=%errorlevel%"

echo.
echo ============================================================
echo HES Power Outage Agent exited with code %EXITCODE%
echo ============================================================
pause
exit /b %EXITCODE%
