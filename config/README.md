# Configuration

`config.yaml` controls the HES URL, browser, login selectors, navigation labels, meter-search selector and output processing.

## Automatic login

Use either:

- `HES_USERNAME` and `HES_PASSWORD` environment variables, or
- the Windows DPAPI-protected local credential store created by the first-run setup.

The protected credential file is `config/credentials.dpapi`; it is ignored by Git. Real credentials must never be committed.

If the HES login presents a CAPTCHA, the agent pauses for manual CAPTCHA completion and then continues automatically.

## Browser requirement

The agent uses an installed Google Chrome executable because the HES XLS export has been verified with regular Chrome. `portal.browser.executable_path` can be set explicitly when Chrome is installed in a non-standard location.

## Date parsing

`processing.date_dayfirst` controls ambiguous slash-separated dates. ISO HES timestamps such as `2026-07-31` are interpreted as ISO regardless of this setting. The current default is `false` (month-first) because the current implementation uses the HES export convention observed during development. If a raw HES export demonstrates DD/MM/YYYY, set it to `true`.

## Meter workflow

For each meter the agent performs:

1. Analytics
2. View Meter Data
3. Enter meter number
4. Apply/Search
5. Open the meter
6. Select the requested dataset/sub-view
7. Click XLS and save the downloaded file
8. Normalize HES HTML/XML/CSV exports to a real XLSX when required
9. Return to Analytics -> View Meter Data with retry/session recovery
10. Continue to the next meter/dataset
