"""Moodle-Modul- (Section-)Discovery und -Auswahl auf einer Kurs-Seite.

Der CLI-Begriff "Modul" entspricht Moodles internem "Section" (Topic/
Chapter). Dieses Modul kapselt die Logik, um alle Sections eines Kurses
aufzulisten, einen per Name zu finden und hin zu navigieren.

Die Section-Markup-Varianten unterscheiden sich (z.B. "Allgemein" als
``<li>`` versus die übrigen als Grid-Cards), daher arbeitet die Extraktion
format-agnostisch über alle ``[id^="section-"]``-Elemente.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from playwright.sync_api import Page
from rich.console import Console

console = Console()


@dataclass(frozen=True)
class Module:
    id: str          # äußere Element-ID, z.B. "section-2"
    name: str        # Anzeigename
    url: str         # /course/section.php?id=<sectionid>
    is_current: bool = False


class ModuleError(RuntimeError):
    pass


_MODULES_JS = r"""
() => {
    // Wir nehmen jedes Element mit id="section-N" (egal ob <div> oder <li>)
    // und extrahieren Namen + section.php-URL. Format-agnostisch: funktioniert
    // sowohl für Grid-Karten (section-1+) als auch für die List-Item-Variante
    // (section-0 "Allgemein").
    const out = [];
    const seen = new Set();
    const elements = document.querySelectorAll('[id^="section-"]');
    for (const el of elements) {
        const id = el.id || '';
        if (!/^section-\d+$/.test(id)) continue;  // "section-0-content" o.ä. überspringen
        if (seen.has(id)) continue;
        seen.add(id);

        // Name: title-Attribut > data-sectionname > .sectionname-Text > erster <h3>
        let name = (el.getAttribute('title') || '').trim();
        if (!name) name = (el.getAttribute('data-sectionname') || '').trim();
        if (!name) {
            const sn = el.querySelector('.sectionname');
            if (sn) name = (sn.textContent || '').trim();
        }
        if (!name) {
            const h3 = el.querySelector('h3');
            if (h3) name = (h3.textContent || '').trim();
        }
        if (!name) continue;

        // URL: erster Anker mit /course/section.php?id=…
        const link = el.querySelector('a[href*="/course/section.php?id="]');
        if (!link) continue;
        const url = (link.getAttribute('href') || '').trim();
        if (!url) continue;

        const isCurrent = el.classList.contains('currentgridsection');
        out.push({ id, name, url, is_current: isCurrent });
    }
    return out;
}
"""


def list_modules(page: Page) -> list[Module]:
    """Liest alle Sections der aktuell offenen Kurs-Seite (format-agnostisch)."""
    raw = page.evaluate(_MODULES_JS)
    modules = [
        Module(
            id=item["id"],
            name=item["name"],
            url=item["url"],
            is_current=bool(item.get("is_current", False)),
        )
        for item in raw
    ]
    console.log(f"[module] {len(modules)} Modul(e) auf der Kurs-Seite gefunden.")
    return modules


def find_module(modules: list[Module], target: str) -> Module:
    """Case-insensitive exakter Match gegen den Modul-Namen.

    Wirft ``ModuleError`` bei null oder mehr als einem Treffer.
    """
    t = target.strip().casefold()
    matches = [m for m in modules if m.name.strip().casefold() == t]
    if not matches:
        available = (
            "\n  - " + "\n  - ".join(m.name for m in modules)
            if modules
            else " (keine)"
        )
        raise ModuleError(
            f"Modul '{target}' nicht gefunden. Verfügbar:{available}"
        )
    if len(matches) > 1:
        urls = ", ".join(m.url for m in matches)
        raise ModuleError(
            f"Modul '{target}' ist nicht eindeutig — {len(matches)} Treffer: {urls}"
        )
    return matches[0]


def goto_module(page: Page, module: Module) -> None:
    """Navigiert zur Modul-URL und prüft, dass wir wirklich auf einer
    Section-Seite gelandet sind."""
    page.goto(module.url, wait_until="domcontentloaded", timeout=45_000)
    parsed = urlparse(page.url)
    if "course/section.php" not in (parsed.path or ""):
        raise ModuleError(
            f"Navigation zum Modul '{module.name}' landete unerwartet auf {page.url}."
        )
