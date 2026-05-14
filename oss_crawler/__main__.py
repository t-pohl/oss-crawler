"""CLI entry point for oss-crawler.

Aktuelle Iteration: Login + Session-Persistenz + Schul-, Kurs- und Modulauswahl.

# TODO(next): import downloader here once that module exists.
"""
from __future__ import annotations

import argparse
import sys

from rich.console import Console

from .auth import AuthError, authenticated_context
from .config import load_settings
from .course import (
    CourseError,
    find_course,
    get_kurse_link,
    goto_course,
    goto_courses_dashboard,
    list_courses,
)
from .module import (
    ModuleError,
    find_module,
    goto_module,
    list_modules,
)
from .school import (
    SCHOOL_ALIASES,
    SchoolError,
    list_schools,
    resolve_school,
    switch_school,
)

console = Console()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    aliases_help = ", ".join(f"'{k}' ({v})" for k, v in SCHOOL_ALIASES.items())
    p = argparse.ArgumentParser(
        prog="oss-crawler",
        description=(
            "Crawler für Online-Schule Saarland (Moodle/Shibboleth-SSO). "
            "Aktuelle Iteration: Login, Session-Persistenz, Schul-, Kurs- "
            "und Modulauswahl."
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
    p.add_argument(
        "--school",
        metavar="ALIAS_OR_NAME",
        help=(
            "Auf eine bestimmte Schule wechseln. Aliase: "
            f"{aliases_help}. Alternativ den vollen Schulnamen wie "
            "im Schulwechsel-Menü angeben."
        ),
    )
    p.add_argument(
        "--list-schools",
        action="store_true",
        help=(
            "Verfügbare Schulen des Accounts ausgeben (Namen aus dem "
            "Schulwechsel-Menü) und beenden."
        ),
    )
    p.add_argument(
        "--course",
        metavar="FULL_NAME",
        help=(
            "Kurs anhand seines vollen Namens (case-insensitive) auswählen. "
            "Navigiert zur Kurs-Seite und beendet."
        ),
    )
    p.add_argument(
        "--list-courses",
        action="store_true",
        help=(
            "Alle Kurse der aktiven Schule ausgeben (ein Name pro Zeile) "
            "und beenden."
        ),
    )
    p.add_argument(
        "--module",
        metavar="FULL_NAME",
        help=(
            "Modul (Abschnitt) anhand seines vollen Namens (case-insensitive) "
            "auswählen. Erfordert --course. Navigiert zur Modul-Seite und beendet."
        ),
    )
    p.add_argument(
        "--list-modules",
        action="store_true",
        help=(
            "Alle Module des aktiven Kurses ausgeben (ein Name pro Zeile) "
            "und beenden. Erfordert --course."
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

    if (args.list_modules or args.module) and not args.course:
        console.print(
            "[red]--module / --list-modules erfordert --course "
            "in derselben Invocation.[/red]"
        )
        return 5

    try:
        with authenticated_context(settings, force_login=args.login) as ctx:
            if args.list_schools:
                page = ctx.new_page()
                try:
                    page.goto(
                        f"{settings.oss_base_url}/",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    for name in list_schools(page):
                        print(name)
                finally:
                    page.close()
                return 0

            if args.school:
                target = resolve_school(args.school)
                page = ctx.new_page()
                try:
                    page.goto(
                        f"{settings.oss_base_url}/",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    switch_school(page, target)
                    console.print(f"[green]Aktive Schule: {target}[/green]")
                finally:
                    page.close()

            if args.list_courses or args.course:
                page = ctx.new_page()
                try:
                    page.goto(
                        f"{settings.oss_base_url}/",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    kurse_link = get_kurse_link(page)
                    goto_courses_dashboard(page, kurse_link)
                    courses = list_courses(page)

                    if args.list_courses:
                        for c in courses:
                            print(c.name)
                        return 0

                    target_course = find_course(courses, args.course)
                    goto_course(page, target_course)
                    console.print(
                        f"[green]Kurs: {target_course.name} — {target_course.url}[/green]"
                    )

                    if args.list_modules or args.module:
                        modules = list_modules(page)
                        if args.list_modules:
                            for m in modules:
                                print(m.name)
                            return 0
                        target_module = find_module(modules, args.module)
                        goto_module(page, target_module)
                        console.print(
                            f"[green]Modul: {target_module.name} — "
                            f"{target_module.url}[/green]"
                        )
                finally:
                    page.close()

            console.print(
                f"[green]Auth OK. Session: {settings.auth_state_path}[/green]"
            )
            return 0
    except AuthError as e:
        console.print(f"[red]Auth failed: {e}[/red]")
        return 2
    except SchoolError as e:
        console.print(f"[red]Schulauswahl fehlgeschlagen: {e}[/red]")
        return 3
    except CourseError as e:
        console.print(f"[red]Kursauswahl fehlgeschlagen: {e}[/red]")
        return 4
    except ModuleError as e:
        console.print(f"[red]Modulauswahl fehlgeschlagen: {e}[/red]")
        return 5
    except KeyboardInterrupt:
        console.print("[yellow]Abgebrochen.[/yellow]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
