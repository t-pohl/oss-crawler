"""Datei- und Verzeichnisname-Sanitisierung.

Portiert nach Python die Regeln aus
``/home/thomas/Repos/linux-config/aliases/alias-scripts/sanitizeNames/sanitizeNames.sh``.

Schritte:
1. Mehrfache Leerzeichen → ``_``.
2. Umlaute ersetzen (``ä→ae``, …) — inkl. NFD- und Mojibake-Varianten.
   2b. Restliche lateinische/französische Akzente strippen (``é→e``, ``ç→c``, …).
   2c. Typografische Sonderzeichen normalisieren (En-Dash → ``-``, „/“ entfernen).
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
import unicodedata


_UMLAUT_REPLACEMENTS: list[tuple[str, str]] = [
    ("ä", "ae"), ("ö", "oe"), ("ü", "ue"),
    ("Ä", "AE"), ("Ö", "OE"), ("Ü", "UE"),
    ("ß", "ss"),
]

# Kaputte/Mojibake-Umlaut-Kodierungen aus fehlerhaften Codepage-Round-Trips.
# Nur diese Fälle treten in echten Daten auf:
#  - ``ü`` als ``u`` + mis-dekodiertes kombinierendes Trema (U+2560 U+0438, „╠и“);
#    der Grundvokal bleibt ein ASCII-``u`` davor stehen.
#  - ``ö`` als einzelnes kyrillisches „ф“ (U+0444), z. B. „lфsung“ → „loesung“.
_MOJIBAKE_REPLACEMENTS: list[tuple[str, str]] = [
    ("u╠и", "ue"),
    ("ф", "oe"),
]

# Restliche lateinische/französische Akzente (Aigu, Grave, Circonflexe, Tréma,
# Cédille) auf den Grundbuchstaben strippen. Läuft NACH der Umlaut-Behandlung,
# deshalb sind deutsche ä/ö/ü/ß hier absichtlich nicht enthalten — ein
# französisches Tréma (ë/ï/ÿ) verliert den Akzent, ein deutsches nicht.
_ACCENT_REPLACEMENTS: list[tuple[str, str]] = [
    ("à", "a"), ("á", "a"), ("â", "a"), ("À", "A"), ("Á", "A"), ("Â", "A"),
    ("è", "e"), ("é", "e"), ("ê", "e"), ("ë", "e"),
    ("È", "E"), ("É", "E"), ("Ê", "E"), ("Ë", "E"),
    ("ì", "i"), ("í", "i"), ("î", "i"), ("ï", "i"),
    ("Ì", "I"), ("Í", "I"), ("Î", "I"), ("Ï", "I"),
    ("ò", "o"), ("ó", "o"), ("ô", "o"), ("Ò", "O"), ("Ó", "O"), ("Ô", "O"),
    ("ù", "u"), ("ú", "u"), ("û", "u"), ("Ù", "U"), ("Ú", "U"), ("Û", "U"),
    ("ý", "y"), ("ÿ", "y"), ("Ý", "Y"), ("Ÿ", "Y"),
    ("ç", "c"), ("Ç", "C"),
]

# Streunende typografische Interpunktion normalisieren.
_PUNCT_REPLACEMENTS: list[tuple[str, str]] = [
    ("–", "-"),   # En-Dash → Bindestrich
    ("„", ""),    # dt. untere Anführungszeichen → entfernen
    ("“", ""),    # linkes Anführungszeichen → entfernen
]

_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MULTI_SPACE = re.compile(r" +")
_MULTI_UNDERSCORE = re.compile(r"_+")

# Deutsche Funktionswörter, die in Verzeichnisnamen klein bleiben (außer am Anfang).
_EXCEPTIONS = frozenset({
    "und", "oder", "von", "zu", "in", "mit", "auf", "fuer",
    "aus", "der", "die", "das", "dem", "den", "des",
    "am", "im", "zum",
})


def _core(name: str) -> str:
    # NFC-Normalisierung zuerst: Netzwerk-Shares (Synology/SMB) liefern Namen
    # mal als NFD (z. B. ``ö`` = ``o`` + kombinierendes Trema). Ohne diese
    # Zusammensetzung würden die Umlaut-Ersetzungen unten nicht greifen.
    s = unicodedata.normalize("NFC", name)
    s = _MULTI_SPACE.sub("_", s)
    for src, dst in _UMLAUT_REPLACEMENTS:
        s = s.replace(src, dst)
    # NFD-Varianten (z. B. ``o`` + kombinierendes Trema) sind durch die
    # NFC-Normalisierung oben bereits zu Präkomposita zusammengefasst und damit
    # von den Umlaut-Ersetzungen abgedeckt. Mojibake-Bytes überleben NFC jedoch
    # und brauchen eine eigene Behandlung.
    for src, dst in _MOJIBAKE_REPLACEMENTS:
        s = s.replace(src, dst)
    for src, dst in _ACCENT_REPLACEMENTS:
        s = s.replace(src, dst)
    for src, dst in _PUNCT_REPLACEMENTS:
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
