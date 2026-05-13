"""Aktive Schule auslesen und über das Schulwechsel-Menü wechseln.

OSS-Accounts können mehreren Schulen zugeordnet sein (z.B. Lehrkräfte mit
Studienseminar + einzelnen Gymnasien). Das Dashboard zeigt die aktive Schule
in ``#badge-school`` an und stellt im linken Sidebar-Menü unter
"Schulwechsel" eine Liste der verfügbaren Schulen als Direkt-Links bereit.

Diese Modul kapselt:
- Auflösung kurzer Aliase (z.B. ``asg``) auf den vollen Schulnamen.
- Auslesen der aktuell aktiven Schule.
- Auflisten aller für den Account verfügbaren Schulen.
- Wechsel auf eine Zielschule, idempotent.
"""
from __future__ import annotations

from urllib.parse import urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from rich.console import Console

console = Console()


SCHOOL_ALIASES: dict[str, str] = {
    "asg": "Albert-Schweitzer-Gymnasium Dillingen",
    "sgs": "Gymnasium am Stadtgarten Saarlouis",
}


class SchoolError(RuntimeError):
    pass


def resolve_school(value: str) -> str:
    """Alias auflösen oder vollen Schulnamen unverändert durchreichen."""
    v = value.strip()
    return SCHOOL_ALIASES.get(v.lower(), v)


def get_current_school(page: Page) -> str:
    """Aktuellen Schulnamen aus ``#badge-school`` lesen (gestrippt).

    Das Badge ist nach DOMContentLoaded zunächst leer und wird erst per JS
    befüllt. Ohne aktives Warten würden wir hier oft einen leeren String
    sehen — und damit die Idempotenz-Prüfung in :func:`switch_school` aushebeln,
    weil der Drawer die aktuell aktive Schule nicht enthält.
    """
    loc = page.locator("#badge-school")
    try:
        loc.wait_for(state="visible", timeout=15_000)
        page.wait_for_function(
            "() => { const el = document.querySelector('#badge-school');"
            " return !!(el && el.textContent && el.textContent.trim().length > 0); }",
            timeout=15_000,
        )
    except PlaywrightTimeoutError:
        # Fall-through: leerer Wert wird dem Aufrufer zurückgegeben.
        pass
    return (loc.text_content() or "").strip()


def _open_school_drawer(page: Page) -> None:
    """Klickt den Sidebar-Eintrag "Schulwechsel" und wartet, bis die
    Schulen-Liste sichtbar ist.

    Der Drawer ist im DOM stets vorhanden (server-rendered); nur die
    ``.hidden``-Klasse am Container wird per JS getoggelt. Statt auf den
    Container zu warten, prüfen wir die Sichtbarkeit eines Listenelements —
    das ist robuster gegen CSS-Varianten.
    """
    page.locator("#switchSchoolMenu a.menuItemLink").click(timeout=10_000)
    try:
        page.locator("#oss-sidebar-dropout .subMenuItem a").first.wait_for(
            state="visible", timeout=10_000
        )
    except PlaywrightTimeoutError as e:
        raise SchoolError(
            "Schulwechsel-Drawer ließ sich nicht öffnen — Sidebar-Markup "
            "hat sich evtl. geändert. Prüfe Selektoren in school.py."
        ) from e


def list_schools(page: Page) -> list[str]:
    """Öffnet den Drawer und gibt die Namen aller verfügbaren Schulen zurück."""
    _open_school_drawer(page)
    names: list[str] = []
    for link in page.locator("#oss-sidebar-dropout .subMenuItem a").all():
        text = (link.text_content() or "").strip()
        if text:
            names.append(text)
    return names


def switch_school(page: Page, target_name: str) -> None:
    """Wechselt auf die Schule mit dem Anzeigenamen ``target_name``.

    - Idempotent: wenn das Badge bereits den Zielnamen zeigt, no-op.
    - Wirft ``SchoolError``, wenn die Schule nicht im Drawer auftaucht oder
      sich das Badge nach dem Wechsel nicht aktualisiert.

    Der Match ist exakte, gestrippte Gleichheit gegen den sichtbaren Text
    der ``<a>``-Einträge im Drawer.
    """
    target = target_name.strip()

    current = get_current_school(page)
    if current == target:
        console.log(f"[school] Bereits auf Schule '{target}' — kein Wechsel nötig.")
        return

    console.log(f"[school] Wechsle von '{current}' zu '{target}'…")

    # OSS-Root vor dem Klick merken — nach dem switchSchool-Redirect kann der
    # SP uns zu einer schulspezifischen LMS-Subdomain weiterleiten (Deep-Link
    # zum letzten Standort), wo das #badge-school nicht existiert. Wir
    # navigieren danach explizit zurück.
    parsed_before = urlparse(page.url)
    oss_root = f"{parsed_before.scheme}://{parsed_before.netloc}/"
    oss_host = parsed_before.hostname

    _open_school_drawer(page)

    schools_seen: list[str] = []
    matched = None
    for link in page.locator("#oss-sidebar-dropout .subMenuItem a").all():
        text = (link.text_content() or "").strip()
        schools_seen.append(text)
        if text == target:
            matched = link
            break

    if matched is None:
        raise SchoolError(
            f"Schule '{target}' nicht im Schulwechsel-Menü gefunden.\n"
            f"Verfügbar: {', '.join(schools_seen) or '(leer)'}"
        )

    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
            matched.click()
    except PlaywrightTimeoutError as e:
        raise SchoolError(
            f"Navigation nach Klick auf '{target}' hat nicht stattgefunden "
            "(Timeout)."
        ) from e

    if urlparse(page.url).hostname != oss_host:
        console.log(
            f"[school] Nach Schulwechsel auf {page.url} gelandet — "
            f"navigiere zurück zu {oss_root}."
        )
        page.goto(oss_root, wait_until="domcontentloaded", timeout=30_000)

    new_current = get_current_school(page)
    if new_current != target:
        raise SchoolError(
            f"Schulwechsel fehlgeschlagen. Erwartet: '{target}', "
            f"aktuell: '{new_current}'."
        )
    console.log(f"[school] Aktive Schule jetzt: '{new_current}'.")
