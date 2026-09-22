@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo HES Power Outage Agent - Setup
echo ============================================================

echo.
call :detect_python
if defined PY_CMD goto :python_ready

echo Python 3.11+ was not found. Attempting automatic installation...
echo.

REM Preferred Windows package manager path.
where winget >nul 2>nul
if %errorlevel%==0 (
    echo Trying Microsoft winget: Python 3.11...
    winget install --id Python.Python.3.11 -e --scope user --accept-package-agreements --accept-source-agreements
    if not errorlevel 1 (
        call :detect_python
        if defined PY_CMD goto :python_ready
    )
)

REM Fallback: download the official CPython installer and install silently.
echo.
echo winget did not provide a usable Python installation.
echo Trying the official Python 3.11.9 installer...
set "PY_INSTALLER=%TEMP%\python-3.11.9-amd64.exe"
where powershell >nul 2>nul
if errorlevel 1 goto :python_fail
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile '%PY_INSTALLER%'"
if errorlevel 1 goto :python_fail
if not exist "%PY_INSTALLER%" goto :python_fail

echo Installing Python for the current Windows user...
"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
if errorlevel 1 goto :python_fail

del /q "%PY_INSTALLER%" >nul 2>nul
call :detect_python
if not defined PY_CMD goto :python_fail

goto :python_ready

:detect_python
set "PY_CMD="
set "PY_EXE="
where py >nul 2>nul
if not errorlevel 1 (
    for /f "tokens=*" %%V in ('py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2^>nul') do set "PY_VER=%%V"
    for /f "tokens=1,2 delims=." %%A in ("!PY_VER!") do (
        set "PY_MAJOR=%%A"
        set "PY_MINOR=%%B"
    )
    if defined PY_MINOR if !PY_MAJOR! GTR 3 set "PY_CMD=py -3"
    if defined PY_MINOR if !PY_MAJOR! EQU 3 if !PY_MINOR! GEQ 11 set "PY_CMD=py -3"
)
if defined PY_CMD exit /b 0

where python >nul 2>nul
if not errorlevel 1 (
    for /f "tokens=*" %%V in ('python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2^>nul') do set "PY_VER=%%V"
    for /f "tokens=1,2 delims=." %%A in ("!PY_VER!") do (
        set "PY_MAJOR=%%A"
        set "PY_MINOR=%%B"
    )
    if defined PY_MINOR if !PY_MAJOR! GTR 3 set "PY_CMD=python"
    if defined PY_MINOR if !PY_MAJOR! EQU 3 if !PY_MINOR! GEQ 11 set "PY_CMD=python"
)
exit /b 0

:python_ready
echo Python detected: %PY_CMD%
%PY_CMD% --version
if errorlevel 1 goto :fail

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY_CMD% -m venv .venv
    if errorlevel 1 goto :fail
)

echo Upgrading pip...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo Installing Python packages...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo Checking for installed Google Chrome...
set "CHROME_FOUND="
if exist "%PROGRAMFILES%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" set "CHROME_FOUND=1"
if not defined CHROME_FOUND (
    echo.
    echo ERROR: Google Chrome was not found.
    echo This agent requires the installed Google Chrome browser for HES XLS exports.
    echo Install Google Chrome from https://www.google.com/chrome/ and rerun setup.
    echo Or set portal.browser.executable_path in config/config.yaml.
    goto :fail
)
echo Google Chrome detected.

echo.
echo SETUP COMPLETE.
echo Run run_agent.bat
pause
exit /b 0

:python_fail
echo.
echo ERROR: Automatic Python installation failed.
echo Please install Python 3.11+ manually from https://www.python.org/downloads/windows/
echo Then rerun setup_windows.bat.
goto :fail

:fail
echo.
echo SETUP FAILED. Read the error above.
pause
exit /b 1
