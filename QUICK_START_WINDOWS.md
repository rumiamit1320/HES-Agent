# HES Power Outage Agent — Windows Quick Start

## Run on another Windows laptop

1. Download the complete project as a ZIP from GitHub.
2. Extract the ZIP to any normal folder.
3. Double-click **INSTALL_AND_RUN.bat**.
4. The script automatically:
   - detects or installs Python 3.12;
   - creates the project's isolated `.venv`;
   - installs `requirements.txt`;
   - installs Playwright Chromium support;
   - checks for Google Chrome and attempts to install it when Windows winget is available;
   - launches the existing HES agent with the existing architecture.
5. On the first HES login, enter the HES username/password when requested and complete CAPTCHA/OTP manually.
6. Later runs reuse the local environment and the persistent HES browser/session behavior implemented by the agent.

## Important

- HES credentials are **not included** in the GitHub project.
- CAPTCHA and OTP remain manual.
- Feeder Excel files, HES downloads, reports, logs, browser profiles, and local credentials remain local to the laptop and are not downloaded from GitHub.
- The script creates a local `.venv`, so Python packages are isolated from other projects.
- Internet access is required during first-time setup.

## Direct project download

Use GitHub's **Code -> Download ZIP** for the source snapshot, then run **INSTALL_AND_RUN.bat** after extraction.

The repository remains the source of the application code; this setup file is only a bootstrap/launcher and does not replace the existing HES automation/reporting architecture.
