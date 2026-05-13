"""Authentifizierung gegen Online-Schule Saarland (Moodle hinter Shibboleth-SSO).

Flow:
- Entry: ``{OSS_BASE_URL}/saml2/login`` (Moodle-SP)
- Redirect zum Shibboleth-IdP (``OSS_IDP_HOST``)
- Nach Login wird die SAMLResponse zurück an den SP gepostet und die
  Moodle-Session-Cookie gesetzt; die Landing-Page ist üblicherweise ``/my/``.

Drei Login-Tiers in dieser Reihenfolge:
1. Bestehende ``.auth.json`` wiederverwenden (Playwright ``storage_state``),
   falls sie noch eine gültige Session hat.
2. Auto-Login mit ``OSS_USERNAME``/``OSS_PASSWORD`` (Form-Fill am IdP).
3. Interaktiver Login (sichtbarer Browser) als Fallback.

Mit ``force_login=True`` wird unabhängig vom State direkt Tier 3 verwendet.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from playwright.sync_api import (
    BrowserContext,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from rich.console import Console

from .config import Settings

console = Console()


LOGIN_SELECTORS = {
    "login_url_path": "/saml2/login",
    # Shibboleth-IdP-Felder (j_username/j_password sind kanonisch),
    # generische Fallbacks für Theme-Varianten.
    "username": (
        'input[name="j_username"], input[name="username"], '
        'input[type="email"], input[autocomplete="username"]'
    ),
    "password": (
        'input[name="j_password"], input[name="password"], '
        'input[type="password"], input[autocomplete="current-password"]'
    ),
    "submit_css": (
        'button[type="submit"], input[type="submit"], '
        'button[name="_eventId_proceed"], input[name="_eventId_proceed"]'
    ),
    "submit_text_candidates": [
        "Anmelden",
        "Einloggen",
        "Login",
        "Sign in",
        "Weiter",
    ],
    "cookie_accept_text_candidates": [
        "Akzeptieren",
        "Alle akzeptieren",
        "Zustimmen",
        "Einverstanden",
        "OK",
        "Accept",
        "Accept all",
    ],
}


# Pfad-Fragmente, die nach erfolgreichem Login NICHT mehr enthalten sein dürfen.
LOGIN_PATH_FRAGMENTS = (
    "/idp/",
    "/saml2/login",
    "/Shibboleth.sso",
    "/login",
    "/auth",
    "/sso",
    "/signin",
    "/sign-in",
    "/anmelden",
)


class AuthError(RuntimeError):
    pass


def _is_login_path(path: str) -> bool:
    p = (path or "").lower()
    return any(frag.lower() in p for frag in LOGIN_PATH_FRAGMENTS)


def _verify_session(context: BrowserContext, settings: Settings) -> bool:
    """Lädt eine geschützte Moodle-Seite und prüft, ob wir eingeloggt sind."""
    oss_host = urlparse(settings.oss_base_url).hostname
    page = context.new_page()
    try:
        # 1. Versuch: Moodle-Dashboard.
        try:
            page.goto(
                f"{settings.oss_base_url}/my/",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
        except PlaywrightTimeoutError:
            return False

        parsed = urlparse(page.url)
        if parsed.hostname != oss_host or _is_login_path(parsed.path or ""):
            # Auf Root probieren, falls /my/ in dieser Moodle-Installation nicht existiert.
            try:
                page.goto(
                    f"{settings.oss_base_url}/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
            except PlaywrightTimeoutError:
                return False
            parsed = urlparse(page.url)
            if parsed.hostname != oss_host or _is_login_path(parsed.path or ""):
                return False

        # Negativ-Check: kein Shibboleth-Login-Formular mehr sichtbar.
        try:
            if page.locator(
                'input[name="j_password"], input[name="j_username"]'
            ).count() > 0:
                return False
        except Exception:
            pass
        return True
    finally:
        page.close()


def _dismiss_cookie_banner(page) -> None:
    """Versuche, einen Cookie-/Consent-Banner zu schließen, falls vorhanden."""
    for label in LOGIN_SELECTORS["cookie_accept_text_candidates"]:
        try:
            btn = page.get_by_role("button", name=label, exact=False).first
            if btn.count() > 0 and btn.is_visible(timeout=500):
                btn.click(timeout=2_000)
                page.wait_for_timeout(500)
                console.log(f"[auth] Cookie-Banner akzeptiert ('{label}').")
                return
        except Exception:
            continue


def _click_submit(page) -> bool:
    """Klickt den Submit-Button. Mehrere Strategien als Fallback."""
    css_loc = page.locator(LOGIN_SELECTORS["submit_css"]).first
    try:
        if css_loc.count() > 0:
            css_loc.click(timeout=5_000)
            return True
    except Exception:
        pass

    for label in LOGIN_SELECTORS["submit_text_candidates"]:
        try:
            btn = page.get_by_role("button", name=label, exact=False).first
            if btn.count() > 0:
                btn.click(timeout=5_000)
                return True
        except Exception:
            continue

    try:
        page.locator(LOGIN_SELECTORS["password"]).first.press("Enter", timeout=2_000)
        return True
    except Exception:
        return False


def _dump_debug(page, suffix: str) -> Path:
    """Schreibt Screenshot + HTML zur Diagnose. Liefert den Screenshot-Pfad."""
    debug_dir = Path(".debug")
    debug_dir.mkdir(parents=True, exist_ok=True)
    shot = debug_dir / f"{suffix}.png"
    html = debug_dir / f"{suffix}.html"
    try:
        page.screenshot(path=str(shot), full_page=True)
        html.write_text(page.content(), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        console.log(f"[auth] Konnte Debug-Dump nicht schreiben: {e}")
    return shot


def _login_with_credentials(context: BrowserContext, settings: Settings) -> None:
    page = context.new_page()
    login_url = f"{settings.oss_base_url}{LOGIN_SELECTORS['login_url_path']}"
    console.log(f"[auth] Öffne Login-Seite: {login_url}")
    page.goto(login_url, wait_until="domcontentloaded", timeout=45_000)

    # Auf den Shibboleth-IdP warten — der SP redirected sofort dorthin.
    try:
        page.wait_for_url(
            lambda u: urlparse(u).hostname == settings.oss_idp_host,
            timeout=30_000,
        )
    except PlaywrightTimeoutError:
        # Falls wir nicht zum IdP redirected wurden, sind wir vielleicht
        # bereits eingeloggt — fortfahren, _verify_session entscheidet später.
        console.log("[auth] Kein IdP-Redirect — eventuell bereits eingeloggt.")
    page.wait_for_load_state("networkidle", timeout=15_000)

    _dismiss_cookie_banner(page)

    try:
        username_loc = page.locator(LOGIN_SELECTORS["username"]).first
        username_loc.wait_for(state="visible", timeout=15_000)
        username_loc.fill(settings.oss_username)

        password_loc = page.locator(LOGIN_SELECTORS["password"]).first
        password_loc.wait_for(state="visible", timeout=15_000)
        password_loc.fill(settings.oss_password)
    except PlaywrightTimeoutError as e:
        shot = _dump_debug(page, "login-fields-missing")
        page.close()
        raise AuthError(
            f"Login-Felder nicht gefunden: {e}\n"
            f"Screenshot: {shot}\n"
            "Passe LOGIN_SELECTORS in oss_crawler/auth.py an."
        ) from e

    if not _click_submit(page):
        shot = _dump_debug(page, "login-submit-missing")
        page.close()
        raise AuthError(
            "Submit-Button konnte nicht gefunden/geklickt werden.\n"
            f"Screenshot: {shot}\n"
            "Passe LOGIN_SELECTORS['submit_css'/'submit_text_candidates'] an."
        )

    # Warten, bis wir den IdP wieder verlassen haben (SAMLResponse-POST zum SP).
    try:
        page.wait_for_url(
            lambda u: urlparse(u).hostname != settings.oss_idp_host,
            timeout=45_000,
        )
    except PlaywrightTimeoutError as e:
        shot = _dump_debug(page, "login-no-navigation")
        page.close()
        raise AuthError(
            "Login schlug fehl: Browser blieb auf dem IdP. "
            "Prüfe Username/Passwort (oder ein CAPTCHA/MFA blockt). "
            f"Screenshot: {shot}\n"
            "Alternative: 'oss-crawler --login' für interaktiven Login."
        ) from e

    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass
    finally:
        if not page.is_closed():
            page.close()


def _load_existing_state(
    pw: Playwright,
    settings: Settings,
) -> BrowserContext | None:
    if not settings.auth_state_path.exists():
        return None
    browser = pw.chromium.launch(headless=settings.headless)
    context = browser.new_context(storage_state=str(settings.auth_state_path))
    if _verify_session(context, settings):
        console.log("[auth] Bestehende Session aus .auth.json wiederverwendet.")
        return context
    console.log("[auth] Gespeicherte Session ist abgelaufen, neuer Login nötig.")
    context.close()
    browser.close()
    return None


def _interactive_login(pw: Playwright, settings: Settings) -> BrowserContext:
    """Öffnet einen sichtbaren Browser; der User loggt sich manuell ein.

    Erkennung des Login-Erfolgs: ein Tab auf dem SP-Host (``OSS_BASE_URL``)
    auf einer Nicht-Login-Route, ohne sichtbares Shibboleth-Password-Feld.
    """
    console.print(
        "[yellow]>>> Interaktiver Login: ein Chromium-Fenster öffnet sich gleich.[/yellow]\n"
        "[yellow]>>> Logge dich ganz normal ein (Username + Passwort, ggf. 2FA).[/yellow]\n"
        "[yellow]>>> Die Session wird automatisch erkannt und gespeichert,[/yellow]\n"
        "[yellow]>>> sobald das Moodle-Dashboard sichtbar ist.[/yellow]"
    )
    browser = pw.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    target_url = f"{settings.oss_base_url}/my/"
    page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)

    oss_host = urlparse(settings.oss_base_url).hostname or "psc.online-schule.saarland"

    deadline = time.time() + 600  # 10 Minuten Gesamttimeout
    last_logged_url: str | None = None

    def _abort_login(message: str) -> None:
        try:
            if not page.is_closed():
                page.close()
        except Exception:
            pass
        try:
            context.close()
        except Exception:
            pass
        try:
            browser.close()
        except Exception:
            pass
        raise AuthError(message)

    # Phase 1: warten, bis wir nachweislich im Login-Flow sind (Redirect zum IdP
    # oder auf eine Login-Route am SP). Erst danach prüfen wir auf Erfolg —
    # so vermeiden wir falsche Erfolgsmeldungen für die initiale /my/-Antwort
    # vor dem Redirect.
    phase1_deadline = time.time() + 25
    in_login_flow = False
    while time.time() < phase1_deadline:
        try:
            if page.is_closed() or not browser.is_connected():
                _abort_login(
                    "Browser-Fenster wurde geschlossen, bevor der Login fertig war."
                )
            current_url = page.url
        except Exception:
            time.sleep(0.5)
            continue

        if current_url != last_logged_url:
            console.log(f"[auth] aktuelle URL: {current_url}")
            last_logged_url = current_url

        parsed = urlparse(current_url)
        if parsed.hostname and parsed.hostname != oss_host:
            console.log(
                "[auth] SSO-Redirect erkannt — bitte einloggen. Sobald du auf "
                f"{oss_host} auf einer Nicht-Login-Seite landest, geht's automatisch weiter."
            )
            in_login_flow = True
            break
        if _is_login_path(parsed.path or ""):
            console.log(
                "[auth] Login-Seite erkannt — bitte einloggen. Sobald du auf "
                "einer Nicht-Login-Seite bist, geht's automatisch weiter."
            )
            in_login_flow = True
            break
        time.sleep(0.5)

    if not in_login_flow:
        console.log(
            "[auth] Innerhalb von 25 s keinen Login-Flow gesehen — Polling läuft trotzdem."
        )

    def _live_url(p) -> str:
        """page.url ist gelegentlich stale (Cross-Origin-Cache). Live-Wert via JS holen."""
        try:
            v = p.evaluate("() => window.location.href")
            if isinstance(v, str) and v:
                return v
        except Exception:
            pass
        try:
            return p.url
        except Exception:
            return ""

    def _scan_pages() -> list[tuple[object, str]]:
        """Liefert [(page, live_url), …] für alle offenen Pages im Context."""
        out: list[tuple[object, str]] = []
        try:
            pages = list(context.pages)
        except Exception:
            pages = [page]
        for p in pages:
            try:
                if p.is_closed():
                    continue
            except Exception:
                continue
            url = _live_url(p)
            if url:
                out.append((p, url))
        return out

    last_heartbeat = time.time()
    seen_urls: set[str] = set()
    success = False

    while time.time() < deadline:
        if not browser.is_connected():
            _abort_login("Browser-Fenster wurde geschlossen, bevor der Login fertig war.")
        try:
            main_closed = page.is_closed()
        except Exception:
            main_closed = True

        live_pages = _scan_pages()
        if main_closed and not live_pages:
            _abort_login("Alle Browser-Tabs wurden geschlossen, bevor der Login fertig war.")

        for _, u in live_pages:
            if u not in seen_urls:
                console.log(f"[auth] aktuelle URL: {u}")
                seen_urls.add(u)
                last_heartbeat = time.time()

        for p, u in live_pages:
            parsed = urlparse(u)
            if parsed.hostname != oss_host:
                continue
            if _is_login_path(parsed.path or ""):
                continue
            try:
                if p.locator('input[name="j_password"]').count() > 0:
                    continue
            except Exception:
                pass
            try:
                p.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
            success = True
            break

        if success:
            break

        if time.time() - last_heartbeat > 30:
            remaining = int(deadline - time.time())
            console.log(
                f"[auth] warte weiter auf Login… ({remaining}s Restzeit, "
                f"{len(live_pages)} offene Tabs). "
                "Schließe das Browserfenster mit dem X um abzubrechen."
            )
            last_heartbeat = time.time()

        time.sleep(1)

    if not success:
        _abort_login(
            "Interaktiver Login: Timeout (10 Minuten) überschritten ohne erfolgreichen Login."
        )

    console.print("[green]>>> Login erkannt. Speichere Session…[/green]")
    settings.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(settings.auth_state_path))
    console.print(f"[green]>>> Session gespeichert: {settings.auth_state_path}[/green]")
    try:
        if not page.is_closed():
            page.close()
    except Exception:
        pass
    return context


@contextmanager
def authenticated_context(
    settings: Settings,
    force_login: bool = False,
) -> Iterator[BrowserContext]:
    """Yieldet einen authentifizierten Playwright-BrowserContext.

    Reihenfolge:
    1. ``force_login=True`` → immer interaktiver Login.
    2. ``.auth.json`` existiert und ist gültig → wiederverwenden.
    3. ``OSS_USERNAME``/``OSS_PASSWORD`` gesetzt → Auto-Login. Bei Fehlschlag
       (z.B. CAPTCHA, MFA, geändertes IdP-Theme) Fallback auf Tier 4.
    4. Interaktiver Login (sichtbarer Browser, manuelles Einloggen).

    Die Session wird in ``settings.auth_state_path`` (Default: ``.auth.json``)
    persistiert — sowohl bei Erfolg eines neuen Logins als auch beim
    sauberen Schließen des Contexts.
    """
    with sync_playwright() as pw:
        context: BrowserContext | None = None

        if force_login:
            context = _interactive_login(pw, settings)
        else:
            context = _load_existing_state(pw, settings)

        if context is None:
            if settings.has_credentials():
                browser = pw.chromium.launch(headless=settings.headless)
                cand = browser.new_context()
                try:
                    _login_with_credentials(cand, settings)
                    if _verify_session(cand, settings):
                        context = cand
                    else:
                        cand.close()
                        browser.close()
                        console.log(
                            "[auth] Auto-Login: Session-Verifikation fehlgeschlagen. "
                            "Wechsle zu interaktivem Login."
                        )
                except AuthError as e:
                    try:
                        cand.close()
                    except Exception:
                        pass
                    try:
                        browser.close()
                    except Exception:
                        pass
                    console.log(
                        f"[auth] Auto-Login fehlgeschlagen ({e}). "
                        "Wechsle zu interaktivem Login."
                    )

            if context is None:
                context = _interactive_login(pw, settings)

            settings.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(settings.auth_state_path))
            console.log(f"[auth] Session gespeichert: {settings.auth_state_path}")

        try:
            yield context
        finally:
            try:
                context.storage_state(path=str(settings.auth_state_path))
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass
