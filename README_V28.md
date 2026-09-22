# v28 — Running Bill download-only recovery

Based on v26.

Only the **Bill Profile - Running Bill** download path was changed.

- First attempt uses the existing XLS downloader unchanged.
- If that specific Running Bill export fails, the agent re-clicks the Running Bill child tab once, waits briefly for the Angular redraw, and invokes the same existing XLS downloader a second time.
- No changes were made to Power Outage, History Bill, Instant Profile, ESW Notifications, other datasets, browser persistence, Ctrl+C handling, report generation, or the general download mechanism.
