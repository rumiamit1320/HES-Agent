# HES Power Outage Agent

Windows/Python automation agent for collecting HES meter datasets and generating feeder and management reports.

## Features

- HES portal automation with Playwright.
- Human-assisted CAPTCHA/OTP login.
- Feeder Excel input and batch workspace management.
- Power Outage, Meter Information, Instant Profile, Bill Profile and other dataset workflows.
- XLS/HTML export normalization.
- Checkpoint/resume support.
- Deduplicated report generation.
- Management and feeder-level Excel reports.

## Security

Do not commit HES credentials, downloaded HES/APDCL data, reports, logs, browser profiles, or feeder workbooks. Credentials are supplied locally through the supported credential mechanism/environment.

## Run

Install dependencies and Playwright, then run `run_agent.bat` or the Python entry point used by the project.

CAPTCHA/OTP remains a manual authentication step.
