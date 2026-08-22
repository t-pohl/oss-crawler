"""Ignore-Liste: Kurse, Module und Materialien, die nie geladen werden.

Format und Semantik sind die der Ignore-Datei von ``syncSharedMaterials``
bzw. ``matsearch`` (ms): ein Muster pro Zeile, ``#`` leitet einen Kommentar
ein, Leerzeilen werden übersprungen, Glob-Wildcards (``*``, ``?``, ``[...]``)
gelten.

- Ohne ``/`` wird das Muster gegen *jede* Pfadkomponente gematcht, ``*.mp4``
  überspringt also solche Dateien überall und ``Archiv`` jeden Ordner dieses
  Namens (Kurs, Modul oder Folder-Aktivität).
- Mit ``/`` wird es gegen den Pfad relativ zur ``--target``-Wurzel gematcht,
  wobei es den ganzen Pfad oder einen beliebigen zusammenhängenden Abschnitt
  abdecken darf: ``Archiv/Alt`` trifft diesen Ordner überall (und alles
  darunter), ``ASG/8 Informatik*/Allgemein`` einen vollen Pfad.

Der Pfad ist dabei ``<Schule>/<Kurs>/<Modul>/<Material>`` — bei Folder-
Aktivitäten ``…/<Modul>/<Folder>/<Datei>``. Jede Komponente wird sowohl gegen
den **OSS-Namen** (wie in ``--list-courses`` / ``--list-modules`` angezeigt)
als auch gegen den **sanitisierten Namen auf der Platte** (inkl. Dateiendung)
gematcht. So funktionieren sowohl ``8 Informatik 2025-26 GRS`` als auch
``8_Informatik_2025-26_GRS`` und ``*.pdf``.

Gematcht wird case-insensitiv — passend zu ``--course``/``--module``, die
Namen ebenfalls case-insensitiv auflösen.

Eine ignorierte Datei wird nur bei künftigen Läufen ausgelassen; bereits
heruntergeladene Kopien bleiben liegen (nichts wird gelöscht).
"""
from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from .sanitize import sanitize_dir_name

console = Console()

#: Dateiname der Ignore-Liste neben der ``.exe`` bzw. im Arbeitsverzeichnis.
IGNORE_FILENAME = "oss-crawler_ignore.txt"

#: Eine Pfadkomponente: ein Name oder mehrere Namensvarianten (roh + sanitisiert).
Component = str | Sequence[str]


def parse_patterns(lines: Iterable[str]) -> list[str]:
    """Liest Muster aus den Zeilen einer Ignore-Datei.

    Umgebende Whitespaces, ein abschließendes CR (CRLF-Dateien von Windows)
    sowie führende/abschließende ``/`` werden getrimmt; Leerzeilen und
    ``#``-Kommentare fallen weg.
    """
    patterns: list[str] = []
    for line in lines:
        s = line.rstrip("\r\n").strip().strip("/")
        if not s or s.startswith("#"):
            continue
        patterns.append(s)
    return patterns


def _variants(component: Component) -> tuple[str, ...]:
    if isinstance(component, str):
        return (component.lower(),)
    # Duplikate raus (roher und sanitisierter Name sind oft identisch),
    # Reihenfolge egal — wir fragen ohnehin nur "irgendeine trifft?".
    return tuple({c.lower() for c in component if c})


def _match_component(pattern: str, variants: tuple[str, ...]) -> bool:
    # ``fnmatchcase`` auf beidseitig kleingeschriebenen Strings statt
    # ``fnmatch``: letzteres normalisiert über ``os.path.normcase`` und wäre
    # damit auf Linux case-sensitiv, auf Windows nicht.
    return any(fnmatch.fnmatchcase(v, pattern) for v in variants)


@dataclass(frozen=True)
class IgnoreList:
    """Kompilierte Muster; ``matches()`` prüft einen relativen Pfad."""

    patterns: tuple[str, ...] = ()
    # Jedes Muster einmal in kleingeschriebene Komponenten zerlegt — ``matches``
    # läuft pro Material, das Splitten muss da nicht jedes Mal passieren.
    _compiled: tuple[tuple[str, ...], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_compiled",
            tuple(
                tuple(p for p in pattern.lower().split("/") if p)
                for pattern in self.patterns
            ),
        )

    def __bool__(self) -> bool:
        return bool(self.patterns)

    def matches(self, *components: Component) -> bool:
        """True, wenn ein Muster den Pfad ``components`` trifft.

        Muster ohne ``/`` bestehen aus einer Komponente und treffen damit an
        jeder Position; mehrteilige Muster müssen einen zusammenhängenden
        Abschnitt abdecken. Da ein Muster auch mitten im Pfad greifen darf,
        ist alles unterhalb eines getroffenen Ordners automatisch mit
        ignoriert.
        """
        if not self.patterns:
            return False
        comps = [_variants(c) for c in components]
        for parts in self._compiled:
            n = len(parts)
            if not n or n > len(comps):
                continue
            for i in range(len(comps) - n + 1):
                if all(
                    _match_component(p, comps[i + j]) for j, p in enumerate(parts)
                ):
                    return True
        return False


def load_ignore_list(path: Path, *, explicit: bool = False) -> IgnoreList:
    """Lädt die Ignore-Datei; eine fehlende Datei ignoriert nichts.

    ``explicit`` markiert einen per ``--ignore-file`` angegebenen Pfad — dann
    ist eine fehlende Datei vermutlich ein Tippfehler und wird gemeldet.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if explicit:
            console.print(f"[yellow]Ignore-Datei nicht gefunden: {path}[/yellow]")
        return IgnoreList()
    except OSError as e:
        console.print(f"[yellow]Ignore-Datei nicht lesbar ({path}): {e}[/yellow]")
        return IgnoreList()
    return IgnoreList(tuple(parse_patterns(raw.splitlines())))


def dir_variants(name: str) -> tuple[str, str]:
    """Match-Kandidaten einer Verzeichniskomponente: OSS-Name + sanitisiert."""
    return (name, sanitize_dir_name(name))
