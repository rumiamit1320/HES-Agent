@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Virtual environment not found.
    echo Starting automatic setup...
    call setup_windows.bat
    if errorlevel 1 exit /b 1
)
echo.
echo ============================================================
echo HES POWER OUTAGE AGENT
echo Automatic HES login is enabled.
echo ============================================================
echo.
echo First run: the agent will ask for HES username/password once.
echo The credentials are then protected with Windows DPAPI.
echo Future runs log in automatically.
echo.
echo CAPTCHA/OTP, if required by HES, must be completed in the browser.
echo.
.venv\Scripts\python.exe -m hes_agent.agent
pause
