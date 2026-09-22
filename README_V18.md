# v18 — HES download + persistent Chrome fix

This build keeps the existing feeder/batch/checkpoint architecture intact.

### Download path
All datasets now use the same Playwright XLS export mechanism that was proven to work for Power Outage. The separate native-Chrome polling path that was timing out for Instant/Bill/Daily/Load has been removed from the active path.

### Dataset validation
Validation was aligned with the actual headers observed in the supplied test log, including:
- Meter Information: Parameter Name / Parameter Value
- Instant Profile: Reading Date, Voltage P1/P2/P3, Current P1/P2/P3, Energy KWH/KVAH
- Event/Tamper Data: Power Off Time, Power On Time, Duration
- Communication Settings: Meter ID, Reading Date, RTC Sync Status/Billing Date

### CTRL+C / browser persistence
Chrome is launched as a detached Windows GUI process and Playwright attaches through CDP. This is deliberate: a console CTRL+C should stop Python's current batch operation without terminating Chrome.

- Completed downloads remain preserved.
- No return-navigation/recovery is performed after CTRL+C.
- No Chrome restart/CAPTCHA is triggered by CTRL+C.
- Chrome is terminated only when the user selects menu option 4 (Exit).
