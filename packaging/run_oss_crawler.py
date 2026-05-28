"""PyInstaller entry script.

PyInstaller runs the entry script as a top-level module (``__name__ ==
"__main__"``, no parent package), which means relative imports inside it
fail. ``oss_crawler/__main__.py`` uses ``from .auth import ...`` and would
crash at startup.

This wrapper avoids that by importing ``oss_crawler.__main__`` as a real
package — same trick setuptools' generated ``oss-crawler`` console-script
uses in dev. Relative imports then resolve normally.
"""
import sys

from oss_crawler.__main__ import _pause_if_frozen, main

if __name__ == "__main__":
    try:
        rc = main()
    finally:
        _pause_if_frozen()
    sys.exit(rc)
