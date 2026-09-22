# Ctrl+C graceful stop fix

Ctrl+C is now handled cooperatively during HES downloads. The signal handler requests a stop, the active Playwright download/polling path checks the request frequently, raises a controlled KeyboardInterrupt at a safe boundary, and the existing batch checkpoint/report/folder-analysis flow runs.

A second Ctrl+C restores the normal immediate interrupt behavior.
