"""CLI entry point for oss-crawler.

This first iteration is login-only: log in to Online-Schule Saarland
(Moodle behind Shibboleth SSO), persist the session to ``.auth.json``,
and verify a protected page is reachable.

# TODO(next): import discovery + downloader here once those modules exist.
"""
from __future__ import annotations

import argparse
import sys

from rich.console import Console

from .auth import AuthError, authenticated_context
from .config import load_settings

console = Console()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="oss-crawler",
        description=(
            "Crawler für Online-Schule Saarland (Moodle/Shibboleth-SSO). "
            "Aktuelle Iteration: Login + Session-Persistenz."
        ),
    )
    p.add_argument(
        "--auth-only",
        action="store_true",
        help=(
            "Einloggen, Session in .auth.json schreiben, beenden. "
            "(Default-Verhalten dieser Iteration.)"
        ),
    )
    p.add_argument(
        "--login",
        action="store_true",
        help=(
            "Erzwingt interaktiven Login im sichtbaren Browser. "
            "Überschreibt eine bestehende .auth.json."
        ),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = load_settings()

    if args.login and settings.auth_state_path.exists():
        try:
            settings.auth_state_path.unlink()
            console.log(
                f"[auth] Bestehende {settings.auth_state_path} entfernt "
                "(--login erzwingt neuen Login)."
            )
        except OSError as e:
            console.print(
                f"[red]Konnte {settings.auth_state_path} nicht entfernen: {e}[/red]"
            )
            return 1

    try:
        with authenticated_context(settings, force_login=args.login):
            console.print(
                f"[green]Auth OK. Session: {settings.auth_state_path}[/green]"
            )
            return 0
    except AuthError as e:
        console.print(f"[red]Auth failed: {e}[/red]")
        return 2
    except KeyboardInterrupt:
        console.print("[yellow]Abgebrochen.[/yellow]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
