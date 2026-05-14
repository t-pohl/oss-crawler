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

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
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
    // Wir kombinieren ZWEI Quellen, damit alle Moodle-Course-Formate
    // abgedeckt sind:
    //   (A) Die Courseindex-Sidebar: ihr <a.courseindex-link>-Element pro
    //       Section ist die zuverlässige Quelle für Section-URLs, auch wenn
    //       die Section-Cards selbst als Modal-Trigger (data-toggle="modal")
    //       statt als <a> gerendert werden.
    //   (B) Die [id="section-N"]-Elemente im Hauptbereich — fallback, falls
    //       die Sidebar in einem Theme fehlt.
    // Dedupliziert wird über die URL.
    const out = [];
    const seenUrls = new Set();

    // (A) Sidebar-Anchors.
    for (const a of document.querySelectorAll(
        'a.courseindex-link[href*="/course/section.php?id="]'
    )) {
        const url = (a.getAttribute('href') || '').trim();
        if (!url || seenUrls.has(url)) continue;
        const name = (a.textContent || '').trim();
        if (!name) continue;
        const parent = a.closest('[data-number]');
        const number = parent ? parent.getAttribute('data-number') : '';
        const id = number ? `section-${number}` : `idx-${url}`;
        seenUrls.add(url);
        out.push({ id, name, url, is_current: false });
    }

    // (B) [id^="section-N"]-Elemente.
    for (const el of document.querySelectorAll('[id^="section-"]')) {
        const id = el.id || '';
        if (!/^section-\d+$/.test(id)) continue;

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

        const link = el.querySelector('a[href*="/course/section.php?id="]');
        if (!link) continue;
        const url = (link.getAttribute('href') || '').trim();
        if (!url || seenUrls.has(url)) continue;
        seenUrls.add(url);

        const isCurrent = el.classList.contains('currentgridsection');
        out.push({ id, name, url, is_current: isCurrent });
    }

    return out;
}
"""


_MODULES_DIAGNOSTIC_JS = r"""
() => {
    const c = document.querySelector('.grid-section');
    return {
        url: window.location.href,
        title: document.title,
        courseindexAnchors: document.querySelectorAll(
            'a.courseindex-link[href*="/course/section.php?id="]'
        ).length,
        anySectionPhpLinks: document.querySelectorAll(
            'a[href*="/course/section.php?id="]'
        ).length,
        sectionEls: document.querySelectorAll('[id^="section-"]').length,
        gridCards: document.querySelectorAll('.grid-section').length,
        courseindexNav: !!document.querySelector('#courseindex, #course-index'),
        sampleGridCard: c ? c.outerHTML.slice(0, 600) : '',
    };
}
"""


def _wait_for_courseindex(page: Page) -> None:
    """Wartet darauf, dass die Courseindex-Sidebar ihre Section-Anchors
    befüllt — manche Moodle-Themes laden diese erst nach DOMContentLoaded
    via AMD nach. Timeout-fall-through ist OK; danach steht zumindest die
    [id^="section-"]-Fallback-Quelle bereit.
    """
    try:
        page.wait_for_function(
            "() => document.querySelectorAll("
            "'a.courseindex-link[href*=\"/course/section.php?id=\"]'"
            ").length > 0",
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        pass


def list_modules(page: Page) -> list[Module]:
    """Liest alle Sections der aktuell offenen Kurs-Seite (format-agnostisch)."""
    _wait_for_courseindex(page)
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
    if len(modules) <= 1:
        diag = page.evaluate(_MODULES_DIAGNOSTIC_JS)
        console.log(f"[module] Diagnose: {diag}")
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
