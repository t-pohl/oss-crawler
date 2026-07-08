"""Material-Discovery und Download für eine Modul- (Section-)Seite.

Liest die Aktivitätsliste (``ul[data-for="cmlist"]``), übersetzt jede
Aktivität in ein :class:`Material` (cmid, name, modtype, view_url,
extension hint), und lädt Dateien runter bzw. legt Shortcut-Dateien an.

Inkrementeller Sync: Skip-Check basiert auf Dateinamen-Existenz im
Zielordner. Kein State-File.
"""
from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

UrlFormat = Literal["linux", "windows"]

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)
from rich.console import Console

from .sanitize import resolve_uuid_names, sanitize_dir_name, sanitize_file_name

console = Console()


@dataclass(frozen=True)
class Material:
    cmid: str
    name: str
    modtype: str
    view_url: str
    ext_hint: str = ""


class MaterialError(RuntimeError):
    pass


@dataclass
class DownloadStats:
    new: int = 0
    skipped: int = 0
    failed: int = 0


_MATERIALS_JS = r"""
() => {
    const list = document.querySelector('ul[data-for="cmlist"], ul.section');
    if (!list) return [];
    const out = [];
    for (const li of list.querySelectorAll('li.activity[id^="module-"]')) {
        const cmid = (li.id || '').replace(/^module-/, '');
        if (!cmid) continue;
        let modtype = '';
        for (const cls of li.classList) {
            if (cls.startsWith('modtype_')) { modtype = cls.slice(8); break; }
        }
        if (!modtype) continue;

        const link = li.querySelector('a.aalink, a.stretched-link');
        const view_url = link ? (link.getAttribute('href') || '').trim() : '';

        let name = '';
        const ins = li.querySelector('.instancename');
        if (ins) {
            const clone = ins.cloneNode(true);
            clone.querySelectorAll('.accesshide').forEach(n => n.remove());
            name = (clone.textContent || '').trim();
        }
        if (!name) {
            const h5 = li.querySelector('.activity-altcontent h5');
            if (h5) name = (h5.textContent || '').trim();
        }
        if (!name) continue;

        let ext_hint = '';
        const badge = li.querySelector('.activitybadge');
        if (badge) ext_hint = (badge.textContent || '').trim().toLowerCase();
        if (!ext_hint) {
            const icon = li.querySelector('img.activityicon');
            if (icon) {
                const src = icon.getAttribute('src') || '';
                const m = src.match(/\/([^/]+)\.svg(?:[?#]|$)/);
                if (m && m[1] !== 'unknown' && m[1] !== 'monologo') {
                    ext_hint = m[1].toLowerCase();
                }
            }
        }

        out.push({ cmid, name, modtype, view_url, ext_hint });
    }
    return out;
}
"""


def list_materials(page: Page) -> list[Material]:
    raw = page.evaluate(_MATERIALS_JS)
    materials = [Material(**r) for r in raw]
    console.log(f"[download] {len(materials)} Materialien gefunden.")
    return materials


def _resource_filename(m: Material) -> str:
    base = sanitize_file_name(m.name)
    ext = m.ext_hint.strip().lower()
    if not ext:
        return base
    if not ext.startswith("."):
        ext = "." + ext
    return base + ext


def _url_filename(m: Material, url_format: UrlFormat) -> str:
    ext = ".html" if url_format == "linux" else ".url"
    return sanitize_file_name(m.name) + ext


def _write_url_shortcut(
    target: Path, external_url: str, display_name: str, url_format: UrlFormat
) -> None:
    if url_format == "windows":
        # Windows ([InternetShortcut]) — auf Linux per `cat` lesbar, aber
        # kein File-Manager öffnet das per Doppelklick im Browser.
        target.write_text(
            f"[InternetShortcut]\nURL={external_url}\n",
            encoding="utf-8",
        )
        return
    # Linux/cross-platform: minimales HTML mit Meta-Refresh. Doppelklick im
    # File-Manager öffnet die Datei in der Standard-Browser-Anwendung, die
    # dann den Refresh-Header auswertet und zur externen URL springt.
    # (Wir verwenden bewusst NICHT .desktop Type=Link — GNOME/Nautilus
    # behandelt das wegen Trust-/Exec-Bit-Heuristiken unzuverlässig.)
    safe_url = html_lib.escape(external_url, quote=True)
    safe_name = html_lib.escape(display_name)
    target.write_text(
        "<!DOCTYPE html>\n"
        "<html lang=\"de\"><head>\n"
        "<meta charset=\"utf-8\">\n"
        f"<meta http-equiv=\"refresh\" content=\"0; url={safe_url}\">\n"
        f"<title>{safe_name}</title>\n"
        "</head><body>\n"
        f"<p>Weiterleitung zu <a href=\"{safe_url}\">{safe_name}</a>…</p>\n"
        "</body></html>\n",
        encoding="utf-8",
    )


_CD_FILENAME = re.compile(
    r"filename\*=(?:UTF-8'')?\"?([^\";]+)\"?|filename=\"?([^\";]+)\"?",
    re.IGNORECASE,
)


def _filename_from_content_disposition(cd: str | None) -> str:
    if not cd:
        return ""
    m = _CD_FILENAME.search(cd)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").strip()


def _ext_from_filename(fname: str) -> str:
    if not fname or "." not in fname:
        return ""
    return "." + fname.rsplit(".", 1)[1].lower()


def _download_resource(
    context: BrowserContext, m: Material, target_dir: Path
) -> tuple[Path, bool]:
    """Lädt ein Resource-Material runter. Liefert (Pfad, was_downloaded).

    Wir umgehen den Browser und ziehen die Datei direkt per
    ``context.request.get`` — gleiche Cookies, aber Chromium öffnet keine
    PDFs/Bilder/etc. in einem neuen Tab. Das umgeht Moodle-Konfigurationen
    bei denen ``forcedownload=1`` nicht greift (z.B. Display-Mode "embed").
    """
    expected = target_dir / _resource_filename(m)
    if expected.exists():
        return expected, False

    sep = "&" if "?" in m.view_url else "?"
    dl_url = f"{m.view_url}{sep}forcedownload=1"

    try:
        response = context.request.get(dl_url, timeout=90_000)
    except (PlaywrightError, PlaywrightTimeoutError) as e:
        raise MaterialError(
            f"HTTP-Fehler beim Download von '{m.name}' (cmid {m.cmid}): {e}"
        ) from e

    if not response.ok:
        raise MaterialError(
            f"HTTP {response.status} für '{m.name}' (cmid {m.cmid})."
        )

    target = expected
    if "." not in _resource_filename(m):
        # Unbekannte Extension — aus Content-Disposition ableiten.
        cd = response.headers.get("content-disposition", "")
        ext = _ext_from_filename(_filename_from_content_disposition(cd))
        if ext:
            target = target_dir / (sanitize_file_name(m.name) + ext)
            if target.exists():
                return target, False

    target.write_bytes(response.body())
    return target, True


_URL_EXTRACT_JS = r"""
() => {
    const here = window.location.hostname;
    const links = document.querySelectorAll(
        '#region-main a[href], .urlworkaround a[href], .resourcecontent a[href]'
    );
    for (const a of links) {
        const href = a.getAttribute('href') || '';
        if (/^https?:\/\//i.test(href) && !href.includes(here)) {
            return href;
        }
    }
    return '';
}
"""


_FOLDER_FILES_JS = r"""
() => {
    // Folder-Aktivitäten listen Dateien als <a href="https://…/pluginfile.php/…/mod_folder/…">
    // Wir scopen auf den Hauptinhalt, damit eventuelle Header-/Icon-Links
    // außen vor bleiben.
    const root = document.querySelector('#region-main') || document.body;
    const out = [];
    const seen = new Set();
    for (const a of root.querySelectorAll(
        'a[href*="/pluginfile.php/"][href*="/mod_folder/"]'
    )) {
        const url = (a.getAttribute('href') || '').trim();
        if (!url || seen.has(url)) continue;
        seen.add(url);
        let name = '';
        try {
            const u = new URL(url, window.location.href);
            const parts = u.pathname.split('/');
            name = decodeURIComponent(parts[parts.length - 1] || '');
        } catch (e) {}
        if (!name) name = (a.textContent || '').trim();
        if (!name) continue;
        out.push({ name, url });
    }
    return out;
}
"""


def _download_folder(
    context: BrowserContext, m: Material, target_dir: Path, only_new: bool = False
) -> DownloadStats:
    """Lädt alle Dateien eines Moodle-Folder-Materials runter.

    Erzeugt einen Unterordner ``<m.name>`` im Modul-Zielordner und legt
    die einzelnen Dateien darin ab. Per-Datei-Skip-Check anhand der
    Dateinamen-Existenz, wie bei Resources.
    """
    folder_subdir = _ci_dir(target_dir, sanitize_dir_name(m.name))

    page = context.new_page()
    try:
        try:
            page.goto(m.view_url, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightTimeoutError as e:
            raise MaterialError(
                f"Folder-Seite für '{m.name}' (cmid {m.cmid}) nicht erreichbar."
            ) from e
        files = page.evaluate(_FOLDER_FILES_JS)
    finally:
        page.close()

    stats = DownloadStats()
    if not files:
        console.log(f"[download]  ~ {m.name}/ (leerer Ordner, übersprungen)")
        return stats

    # Subdir erst anlegen, sobald wir wissen, dass es Inhalt gibt — verhindert
    # leere Folder-Ordner für OSS-Folder ohne Dateien.
    folder_subdir.mkdir(parents=True, exist_ok=True)

    # UUIDs aus den Dateinamen entfernen, aber bei Kollision innerhalb dieser
    # Ordner-Charge die UUID behalten (Duplikat-Schutz, deterministisch).
    filenames = resolve_uuid_names([f["name"] for f in files], sanitize_file_name)
    for f, filename in zip(files, filenames):
        if not filename:
            continue
        target = folder_subdir / filename
        rel = f"{folder_subdir.name}/{filename}"
        if target.exists():
            stats.skipped += 1
            if not only_new:
                console.log(f"[download]  = {rel} (skip)")
            continue
        try:
            resp = context.request.get(f["url"], timeout=90_000)
        except (PlaywrightError, PlaywrightTimeoutError) as e:
            stats.failed += 1
            console.log(f"[download][red]  ! {rel}: {e}[/red]")
            continue
        if not resp.ok:
            stats.failed += 1
            console.log(f"[download][red]  ! {rel}: HTTP {resp.status}[/red]")
            continue
        target.write_bytes(resp.body())
        stats.new += 1
        console.log(f"[download]  + {rel}")
    return stats


def _download_url_shortcut(
    context: BrowserContext,
    m: Material,
    target_dir: Path,
    url_format: UrlFormat,
) -> tuple[Path, bool]:
    """Schreibt eine Shortcut-Datei (.desktop oder .url je nach Format)."""
    target = target_dir / _url_filename(m, url_format)
    if target.exists():
        return target, False

    page = context.new_page()
    try:
        try:
            page.goto(m.view_url, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightTimeoutError as e:
            raise MaterialError(
                f"URL-View-Seite für '{m.name}' (cmid {m.cmid}) nicht erreichbar."
            ) from e

        lms_host = urlparse(m.view_url).hostname
        cur_host = urlparse(page.url).hostname
        external = ""
        if cur_host and cur_host != lms_host:
            external = page.url
        else:
            external = page.evaluate(_URL_EXTRACT_JS) or ""

        if not external:
            raise MaterialError(
                f"Externe URL für '{m.name}' (cmid {m.cmid}) nicht ermittelbar."
            )

        _write_url_shortcut(target, external, m.name, url_format)
        return target, True
    finally:
        page.close()


def _ci_dir(parent: Path, name: str) -> Path:
    """Löst ``name`` gegen bereits vorhandene Geschwister case-insensitiv auf.

    Auf case-insensitiven Sync-Volumes (Synology Drive, SMB, exFAT) kollidiert
    ein nur in der Groß-/Kleinschreibung abweichender neuer Ordner mit einem
    bereits vorhandenen — z. B. produziert der Sanitizer ``10_IT_Sicherheit``
    (Abkürzung ``IT`` bleibt groß), während vom NAS ein älteres
    ``10_It_Sicherheit`` gesynct wurde. Der Sync-Client betrachtet beide als
    denselben Ordner und benennt den frisch angelegten *während des Laufs* um
    (``…_CaseConflict``), wodurch bereits gecachte Zielpfade ins Leere zeigen
    und der Download mit ``FileNotFoundError`` abbricht.

    Existiert bereits ein Geschwister mit gleichem Namen (case-insensitiv),
    verwenden wir dessen tatsächliche Schreibweise, statt einen zweiten
    Case-Zwilling anzulegen.
    """
    try:
        existing = {p.name.lower(): p.name for p in parent.iterdir() if p.is_dir()}
    except OSError:
        existing = {}
    return parent / existing.get(name.lower(), name)


def _resolve_dir_ci(root: Path, *names: str) -> Path:
    """Baut ``root/names[0]/names[1]/…`` und löst jede Komponente case-insensitiv
    gegen bereits vorhandene Verzeichnisse auf (siehe :func:`_ci_dir`)."""
    path = root
    for name in names:
        path = _ci_dir(path, name)
    return path


def _prune_empty_dirs_up_to(path: Path, stop_at: Path) -> None:
    """Entfernt ``path``, falls leer; läuft danach im Baum nach oben weiter,
    bis ein nicht-leeres Verzeichnis erreicht wird oder ``stop_at`` ansteht.

    ``stop_at`` selbst wird NICHT entfernt (das ist die ``--target``-Wurzel).
    """
    try:
        path = path.resolve()
        stop_at = stop_at.resolve()
    except OSError:
        return
    while path != stop_at:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def download_module(
    context: BrowserContext,
    page: Page,
    school_name: str,
    course_name: str,
    module_name: str,
    root_dir: Path | None = None,
    url_format: UrlFormat = "linux",
    only_new: bool = False,
) -> DownloadStats:
    """Lädt alle (neuen) Materialien des aktuell geöffneten Moduls runter.

    ``page`` muss auf der section.php-Seite des Moduls sein. Zielordner:
    ``root/<school>/<course>/<module>/`` (alle Komponenten sanitisiert).

    Wenn das Modul keine herunterladbaren Materialien enthält (nur Labels,
    leere Folder-Aktivitäten, alle fehlgeschlagen …), wird der angelegte
    Modul-Ordner am Ende wieder entfernt — bis hinauf zur ``--target``-Wurzel,
    falls auch der Kurs-Ordner damit leer wird.
    """
    root = root_dir or Path.cwd()
    target_dir = _resolve_dir_ci(
        root,
        sanitize_dir_name(school_name),
        sanitize_dir_name(course_name),
        sanitize_dir_name(module_name),
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    console.log(f"[download] Zielordner: {target_dir}")

    materials = list_materials(page)
    stats = DownloadStats()

    for m in materials:
        try:
            if m.modtype == "resource":
                path, downloaded = _download_resource(context, m, target_dir)
            elif m.modtype == "url":
                path, downloaded = _download_url_shortcut(
                    context, m, target_dir, url_format
                )
            elif m.modtype == "folder":
                folder_stats = _download_folder(
                    context, m, target_dir, only_new=only_new
                )
                stats.new += folder_stats.new
                stats.skipped += folder_stats.skipped
                stats.failed += folder_stats.failed
                continue
            else:
                continue
            if downloaded:
                stats.new += 1
                console.log(f"[download]  + {path.name}")
            else:
                stats.skipped += 1
                if not only_new:
                    console.log(f"[download]  = {path.name} (skip)")
        except MaterialError as e:
            stats.failed += 1
            console.log(f"[download][red]  ! {m.name}: {e}[/red]")

    # Aufräumen: leere Modul-(und ggf. Kurs-)Ordner wieder entfernen.
    _prune_empty_dirs_up_to(target_dir, root)

    return stats
