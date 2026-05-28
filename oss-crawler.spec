# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone Windows build of oss-crawler.

Produces a single `oss-crawler.exe` (~150-250 MB) that bundles Python,
all dependencies, and the Playwright Chromium browser. Built on Windows
via the GitHub Actions workflow in `.github/workflows/windows-build.yml`.

Build prerequisites (run in this order, on Windows):
    set PLAYWRIGHT_BROWSERS_PATH=0
    pip install -e ".[build]"
    playwright install chromium
    pyinstaller oss-crawler.spec

The matching `PLAYWRIGHT_BROWSERS_PATH=0` at runtime is set by the runtime
hook at `packaging/pyi_rth_playwright.py`.
"""
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# Pull in everything Playwright ships inside its package (driver binary,
# .local-browsers/ if PLAYWRIGHT_BROWSERS_PATH=0 was used at install time,
# README/LICENSE, etc.). `pyinstaller-hooks-contrib` provides the base hook;
# collect_data_files makes sure no files are missed.
playwright_datas = collect_data_files("playwright", include_py_files=False)
playwright_binaries = collect_dynamic_libs("playwright")


a = Analysis(
    ["oss_crawler/__main__.py"],
    pathex=[],
    binaries=playwright_binaries,
    datas=playwright_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["packaging/pyi_rth_playwright.py"],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="oss-crawler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
