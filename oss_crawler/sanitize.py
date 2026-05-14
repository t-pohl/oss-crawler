"""Datei- und Verzeichnisname-Sanitisierung.

Portiert nach Python die Regeln aus
``/home/thomas/Repos/linux-config/aliases/alias-scripts/sanitizeNames/sanitizeNames.sh``.

Schritte:
1. Mehrfache Leerzeichen → ``_``.
2. Umlaute ersetzen (``ä→ae``, …).
3. exFAT-verbotene Zeichen entfernen (``\\ / : * ? " < > |`` plus 0x00-0x1F).
4. Awkward-Combos aufräumen (``_+_``, ``-_``, ``_-``, ``_,``, ``,_``, ``__``…).
5. Casing:
   - Verzeichnis: ``Upper_Snake_Case`` mit zwei Ausnahmen — komplett
     großgeschriebene oder numerische Tokens (Abkürzungen) bleiben
     unverändert, und deutsche Funktionswörter werden nicht am Anfang
     kleingeschrieben.
   - Datei: komplett kleingeschrieben.
6. Leeres Ergebnis → ``unnamed``.
"""
from __future__ import annotations

import re


_UMLAUT_REPLACEMENTS: list[tuple[str, str]] = [
    ("ä", "ae"), ("ö", "oe"), ("ü", "ue"),
    ("Ä", "AE"), ("Ö", "OE"), ("Ü", "UE"),
    ("ß", "ss"),
]
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MULTI_SPACE = re.compile(r" +")
_MULTI_UNDERSCORE = re.compile(r"_+")

# Deutsche Funktionswörter, die in Verzeichnisnamen klein bleiben (außer am Anfang).
_EXCEPTIONS = frozenset({
    "und", "oder", "von", "zu", "in", "mit", "auf", "fuer",
    "der", "die", "das", "dem", "den", "des",
    "am", "im", "zum",
})


def _core(name: str) -> str:
    s = _MULTI_SPACE.sub("_", name)
    for src, dst in _UMLAUT_REPLACEMENTS:
        s = s.replace(src, dst)
    s = _FORBIDDEN.sub("", s)
    s = s.replace("_+_", "+")
    for combo in ("_,", ",_", "-_", "_-"):
        s = s.replace(combo, "_")
    s = _MULTI_UNDERSCORE.sub("_", s)
    return s


def _title_case_word(w: str, *, is_first: bool) -> str:
    if not w:
        return w
    if w == w.upper():
        # Abkürzung / nur Zahlen-und-Bindestrich → unverändert.
        return w
    if not is_first and w.lower() in _EXCEPTIONS:
        return w.lower()
    return w[:1].upper() + w[1:].lower()


def sanitize_dir_name(name: str) -> str:
    s = _core(name)
    if not s:
        return "unnamed"
    parts = s.split("_")
    return "_".join(
        _title_case_word(p, is_first=(i == 0)) for i, p in enumerate(parts)
    )


def sanitize_file_name(name: str) -> str:
    s = _core(name)
    if not s:
        return "unnamed"
    return s.lower()
