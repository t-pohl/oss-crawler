"""Kurs-Discovery und -Auswahl auf dem schul-spezifischen Moodle.

Nach Schulwechsel zeigt die OSS-Sidebar auf eine schul-spezifische Moodle-
Instanz (z.B. ``https://lms-gym-albert-schweitzer.online-schule.saarland/``).
Dieses Modul liest die Kurs-Liste vom Dashboard, matcht einen Zielkurs
gegen den vollen Namen (case-insensitive), und navigiert hin.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from rich.console import Console

from .auth import AuthError, submit_idp_login_form
from .config import Settings, app_dir

console = Console()


@dataclass(frozen=True)
class Course:
    name: str          # aus dem title-Attribut von .multiline
    url: str           # /course/view.php?id=<id>
    category: str = ""


class CourseError(RuntimeError):
    pass


def get_kurse_link(page: Page) -> str:
    """Liest den 'Kurse'-Sidebar-Link auf der OSS-Übersicht."""
    loc = page.get_by_role("link", name="Kurse", exact=True).first
    try:
        loc.wait_for(state="attached", timeout=15_000)
    except PlaywrightTimeoutError as e:
        raise CourseError(
            "'Kurse'-Sidebar-Link nicht gefunden. "
            "Bist du auf der OSS-Übersicht und eingeloggt?"
        ) from e
    href = loc.get_attribute("href")
    if not href:
        raise CourseError("'Kurse'-Sidebar-Link hat kein href-Attribut.")
    return href


def _dump_idp_debug(page: Page, suffix: str) -> None:
    """Speichert Screenshot + HTML der aktuellen Seite zur Diagnose."""
    debug_dir = app_dir() / ".debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    shot = debug_dir / f"{suffix}.png"
    html = debug_dir / f"{suffix}.html"
    try:
        page.screenshot(path=str(shot), full_page=True)
        html.write_text(page.content(), encoding="utf-8")
        console.log(f"[course] Debug-Dump: {shot} / {html}")
    except Exception as e:
        console.log(f"[course] Debug-Dump fehlgeschlagen: {e}")


def _walk_through_idp_consent(
    page: Page, settings: Settings, max_steps: int = 6
) -> None:
    """Klick-Walk durch Shibboleth-Zwischenseiten beim Wechsel zu einem
    neuen SP (z.B. erste LMS-Subdomain einer Schule).

    Handhabt drei Arten von Zwischenseiten:
    1. **Re-Auth (Passwort-Feld)**: füllt das IdP-Form aus den OSS-Credentials
       und sendet ab. Manche IdPs verlangen Re-Auth pro SP, auch wenn schon
       eine IdP-Session existiert.
    2. **Consent / Attribute-Release**: klickt den Accept-Button (bevorzugt
       ``_eventId_proceed``; sonst plausible Accept-Submits) — NIEMALS einen
       Reject/Cancel-Button.
    3. **Sonstiges**: bricht ab, dumpt Screenshot + HTML nach ``.debug/``.

    Max. ``max_steps`` Iterationen gegen Endlosschleifen.
    """
    for step in range(max_steps):
        parsed = urlparse(page.url)
        path = parsed.path or ""
        if "/idp/" not in path:
            return

        # (1) Re-Auth-Fall: Passwort-Feld da → ausfüllen + absenden.
        try:
            has_password_field = (
                page.locator('input[type="password"]').count() > 0
            )
        except Exception:
            has_password_field = False
        if has_password_field:
            console.log(
                f"[course] IdP-Zwischenseite (Step {step + 1}): "
                "Re-Auth-Form, fülle Credentials aus."
            )
            try:
                submit_idp_login_form(page, settings)
            except AuthError as e:
                _dump_idp_debug(page, f"idp-relogin-step{step + 1}")
                raise CourseError(
                    f"Re-Auth auf der IdP fehlgeschlagen: {e}"
                ) from e
            continue

        clicked = False
        # NUR positive Accept-Submits — keine blinden Fallbacks.
        for selector in (
            'input[name="_eventId_proceed"]',
            'button[name="_eventId_proceed"]',
            'input[type="submit"][name="_eventId_proceed"]',
            'input[type="submit"][value*="kzeptieren" i]',
            'input[type="submit"][value*="ortfahren" i]',
            'input[type="submit"][value*="ccept" i]',
            'input[type="submit"][value*="eiter" i]',  # Weiter
            'button:has-text("Akzeptieren")',
            'button:has-text("Fortfahren")',
            'button:has-text("Weiter")',
            'button:has-text("Accept")',
            'button:has-text("Continue")',
        ):
            try:
                btn = page.locator(selector).first
                if btn.count() > 0:
                    btn.click(timeout=5_000)
                    clicked = True
                    console.log(
                        f"[course] IdP-Zwischenseite (Step {step + 1}): "
                        f"'{selector}' geklickt."
                    )
                    break
            except Exception:
                continue

        if not clicked:
            console.log(
                f"[course] IdP-Zwischenseite ohne erkannten Accept-Button: "
                f"{page.url}"
            )
            _dump_idp_debug(page, f"idp-unhandled-step{step + 1}")
            return

        try:
            page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except PlaywrightTimeoutError:
            pass

    # Loop erschöpft, aber wir sind immer noch auf der IdP.
    console.log(
        f"[course] Consent-Walk erschöpft nach {max_steps} Schritten — "
        f"immer noch auf {page.url}"
    )
    _dump_idp_debug(page, "idp-loop-exhausted")


def goto_courses_dashboard(page: Page, kurse_link: str, settings: Settings) -> None:
    """Navigiert zum Moodle-Dashboard (``/my/``) der aktiven Schule.

    Ablauf:
    1. ``kurse_link`` (typisch ``/auth/shibboleth/``) öffnen — sorgt für die
       SAML-Runde an die LMS-Subdomain.
    2. Auf eventuelle IdP-Zwischenseiten reagieren (Re-Auth / Consent), bis
       wir den IdP verlassen haben.
    3. Wohin Moodle danach redirected ist konfigurationsabhängig (Default
       ``/my/``, kann auch ``/`` sein oder Deep-Link). Wir navigieren danach
       EXPLIZIT zu ``{lms_base}/my/`` — idempotent.
    4. Auf den ``courses-view``-Container warten als Stabilitätsanker.
    """
    page.goto(kurse_link, wait_until="domcontentloaded", timeout=60_000)
    console.log(f"[course] nach SAML-Auth auf: {page.url}")

    _walk_through_idp_consent(page, settings)
    if "/idp/" in (urlparse(page.url).path or ""):
        console.log(f"[course] nach Consent-Walk noch auf IdP: {page.url}")

    parsed = urlparse(kurse_link)
    if not parsed.scheme or not parsed.netloc:
        raise CourseError(
            f"'Kurse'-Link hat keine gültige URL-Struktur: {kurse_link!r}"
        )
    lms_dashboard = f"{parsed.scheme}://{parsed.netloc}/my/"

    if not page.url.rstrip("/").endswith("/my"):
        page.goto(lms_dashboard, wait_until="domcontentloaded", timeout=45_000)
        console.log(f"[course] auf Moodle-Dashboard navigiert: {page.url}")

    try:
        page.locator('[data-region="courses-view"]').first.wait_for(
            state="attached", timeout=30_000
        )
    except PlaywrightTimeoutError as e:
        raise CourseError(
            f"Moodle-Dashboard ({lms_dashboard}) hat keinen courses-view-"
            f"Container — aktuelle URL: {page.url}. Evtl. weitere SSO-Schritte "
            "nötig oder Moodle ist anders aufgebaut."
        ) from e


def _set_filter_alle(page: Page) -> None:
    """Setzt den Kurs-Filter auf 'Alle' (idempotent).

    Die Filter-Items leben üblicherweise im Header des "Kursübersicht"-Blocks,
    außerhalb des ``[data-region="courses-view"]``-Containers — daher suchen
    wir im gesamten Dokument.
    """
    already = page.evaluate(
        """() => {
            const a = document.querySelector(
                'a[data-filter="grouping"][data-value="all"]'
            );
            if (!a) return null;
            return a.getAttribute('aria-current') === 'true';
        }"""
    )
    if already is True:
        return
    if already is None:
        console.log(
            "[course] Filter-Eintrag 'Alle' nicht gefunden — überspringe "
            "Filter-Wechsel."
        )
        return

    alle = page.locator(
        'a[data-filter="grouping"][data-value="all"]'
    ).first
    try:
        alle.scroll_into_view_if_needed(timeout=5_000)
        alle.click(timeout=5_000)
    except PlaywrightTimeoutError:
        try:
            alle.click(force=True, timeout=5_000)
        except PlaywrightTimeoutError:
            console.log(
                "[course] Konnte 'Alle' nicht klicken — Filter bleibt unverändert."
            )
            return
    try:
        page.wait_for_function(
            """() => {
                const a = document.querySelector(
                    'a[data-filter="grouping"][data-value="all"]'
                );
                return a && a.getAttribute('aria-current') === 'true';
            }""",
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        pass


_COURSES_JS = r"""
() => {
    // Wir suchen alle Kurs-Links auf dem Dashboard (egal in welchem Block sie
    // liegen) und deduplizieren über die URL. Das deckt sowohl die
    // "Kursübersicht" als auch ggf. "Zuletzt besuchte Kurse" ab, ohne von der
    // Block-Struktur abzuhängen.
    const out = [];
    const seen = new Set();
    const links = document.querySelectorAll(
        'a.coursename[href*="/course/view.php"]'
    );
    for (const link of links) {
        const url = (link.getAttribute('href') || '').trim();
        if (!url || seen.has(url)) continue;

        let name = '';
        const ml = link.querySelector('.multiline');
        if (ml) {
            name = (ml.getAttribute('title') || '').trim();
            if (!name) {
                const span = ml.querySelector('span[aria-hidden="true"]');
                if (span) name = (span.textContent || '').trim();
            }
        }
        if (!name) {
            name = (link.getAttribute('title') || '').trim();
        }
        if (!name) {
            // Fallback: sichtbarer Text-Content des Links, sr-only-Spans
            // ignorierend.
            name = Array.from(link.childNodes)
                .map(n => n.nodeType === Node.TEXT_NODE ? n.textContent : '')
                .join('')
                .trim();
        }
        if (!name) continue;

        seen.add(url);
        const card = link.closest('.course-info-container, .card, .dashboard-card');
        const cat = card ? card.querySelector('.categoryname') : null;
        const category = cat ? (cat.textContent || '').trim() : '';
        out.push({ name, url, category });
    }
    return out;
}
"""


_WAIT_FOR_COURSES_JS = r"""
() => {
    // Strikt: ein Kurs-Link mit *befülltem* Namen muss sichtbar sein.
    // Skelett-Karten haben den Link evtl. schon, aber der innere .multiline
    // ist erst nach AMD-Load gefüllt — daher prüfen wir auf den Titel.
    const links = document.querySelectorAll(
        'a.coursename[href*="/course/view.php"]'
    );
    for (const link of links) {
        const ml = link.querySelector('.multiline');
        if (!ml) continue;
        const title = (ml.getAttribute('title') || '').trim();
        if (title) return true;
        const span = ml.querySelector('span[aria-hidden="true"]');
        if (span && (span.textContent || '').trim()) return true;
    }
    return false;
}
"""


_DIAGNOSTIC_JS = r"""
() => {
    const u = window.location.href;
    const title = document.title;
    const anchors = document.querySelectorAll('a.coursename').length;
    const anchorsWithHref = document.querySelectorAll(
        'a.coursename[href*="/course/view.php"]'
    ).length;
    const containers = document.querySelectorAll('.course-info-container').length;
    const view = document.querySelector('[data-region="courses-view"]');
    const hasView = !!view;
    const hasLoading = !!(view && view.querySelector(
        '[data-region="loading-placeholder"]'
    ));
    const hasNoCoursesImg = !!(view && view.querySelector('img[src*="nocourses"]'));
    return { u, title, anchors, anchorsWithHref, containers,
             hasView, hasLoading, hasNoCoursesImg };
}
"""


def _wait_for_courses_loaded(page: Page) -> None:
    """Wartet, bis mindestens eine Kurs-Karte mit befülltem Namen erscheint.

    Bei echtem Empty-State (Account ohne Kurse) läuft das in den Timeout —
    das ist OK: die anschließende Extraktion liefert dann 0 Kurse mit
    klarer Diagnostik.
    """
    try:
        page.wait_for_function(_WAIT_FOR_COURSES_JS, timeout=25_000)
    except PlaywrightTimeoutError:
        console.log(
            "[course] Timeout (25 s) beim Warten auf befüllte Kurs-Karten — "
            "extrahiere trotzdem."
        )


def list_courses(page: Page) -> list[Course]:
    """Liefert alle Kurse vom Dashboard (Filter wird auf 'Alle' gesetzt).

    Die Extraktion läuft in einem einzigen ``page.evaluate``-Aufruf in JS —
    schneller als pro-Karte-Locator-Calls und ohne implizite 30 s-Waits für
    nicht vorhandene Unter-Elemente.
    """
    _wait_for_courses_loaded(page)
    _set_filter_alle(page)
    _wait_for_courses_loaded(page)
    raw = page.evaluate(_COURSES_JS)
    courses = [
        Course(name=item["name"], url=item["url"], category=item.get("category", ""))
        for item in raw
    ]
    console.log(f"[course] {len(courses)} Kurs(e) auf dem Dashboard gefunden.")
    if not courses:
        diag = page.evaluate(_DIAGNOSTIC_JS)
        console.log(f"[course] Diagnose: {diag}")
    return courses


def find_course(courses: list[Course], target: str) -> Course:
    """Case-insensitive exakter Match gegen den Kursnamen.

    Wirft ``CourseError`` bei null oder mehr als einem Treffer.
    """
    t = target.strip().casefold()
    matches = [c for c in courses if c.name.strip().casefold() == t]
    if not matches:
        available = (
            "\n  - " + "\n  - ".join(c.name for c in courses)
            if courses
            else " (keine)"
        )
        raise CourseError(
            f"Kurs '{target}' nicht gefunden. Verfügbar:{available}"
        )
    if len(matches) > 1:
        urls = ", ".join(c.url for c in matches)
        raise CourseError(
            f"Kurs '{target}' ist nicht eindeutig — {len(matches)} Treffer: {urls}"
        )
    return matches[0]


def goto_course(page: Page, course: Course) -> None:
    """Navigiert zur Kurs-URL und prüft, dass wir wirklich auf der
    Kurs-Seite gelandet sind."""
    page.goto(course.url, wait_until="domcontentloaded", timeout=45_000)
    parsed = urlparse(page.url)
    if "course/view.php" not in (parsed.path or ""):
        raise CourseError(
            f"Navigation zum Kurs '{course.name}' landete unerwartet auf {page.url}."
        )
