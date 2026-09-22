@echo off
setlocal EnableExtensions
title HES Power Outage Agent - Setup and Run

echo ============================================================
echo HES POWER OUTAGE AGENT
echo One-click setup and launch
echo ============================================================
echo.

cd /d "%~dp0"

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
        echo.
        echo ERROR: winget is not available on this Windows installation.
        echo Please install Python 3.12+ manually, then run this file again.
        echo.
        pause
        exit /b 1
    )
    winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
    if %errorlevel% neq 0 (
        echo.
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

if not exist ".venv\Scripts\python.exe" (
    echo Creating isolated Python environment...
    %PYTHON_CMD% -m venv .venv
    if %errorlevel% neq 0 (
        echo ERROR: Could not create the virtual environment.
        pause
        exit /b 1
    )
)

set "VENV_PY=%CD%\.venv\Scripts\python.exe"

echo Upgrading pip...
"%VENV_PY%" -m pip install --upgrade pip
if %errorlevel% neq 0 (
    echo ERROR: pip setup failed.
    pause
    exit /b 1
)

echo Installing project dependencies...
"%VENV_PY%" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo ERROR: Dependency installation failed.
    pause
    exit /b 1
)

echo Installing Playwright browser support...
"%VENV_PY%" -m playwright install chromium
if %errorlevel% neq 0 (
    echo WARNING: Playwright Chromium installation failed.
    echo The agent may still use installed Google Chrome.
)

if not exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" (
    if not exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
        echo.
        echo Google Chrome was not detected.
        echo Attempting automatic Chrome installation with winget...
        where winget >nul 2>&1
        if %errorlevel%==0 (
            winget install --id Google.Chrome -e --source winget --accept-package-agreements --accept-source-agreements
        ) else (
            echo WARNING: winget is not available. Install Google Chrome manually if the agent requests it.
        )
    )
)

echo.
echo ============================================================
echo Starting HES Power Outage Agent
echo ============================================================
echo.
"%VENV_PY%" -m hes_agent.agent
set "EXITCODE=%errorlevel%"

echo.
echo ============================================================
echo HES Power Outage Agent exited with code %EXITCODE%
echo ============================================================
pause
exit /b %EXITCODE%
