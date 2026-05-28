"""PyInstaller runtime hook for Playwright.

`PLAYWRIGHT_BROWSERS_PATH=0` tells Playwright to look for browsers inside
the `playwright` package's own `driver/package/.local-browsers` directory,
which is where the bundled Chromium ends up when we run `playwright install
chromium` with the same env var set at build time. Without this, Playwright
would look in `%LOCALAPPDATA%\\ms-playwright` and fail to find the browser.

Setdefault (not assignment) so a user with their own Chromium install can
still override.
"""
import os

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
