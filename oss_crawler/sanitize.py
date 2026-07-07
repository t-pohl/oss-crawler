"""Datei- und Verzeichnisname-Sanitisierung.

Portiert nach Python die Regeln aus
``/home/thomas/Repos/linux-config/aliases/alias-scripts/sanitizeNames/sanitizeNames.sh``.

Schritte:
0. Kanonische UUIDs (``8-4-4-4-12`` Hex, case-insensitive) entfernen. Läuft
   ganz am Anfang, damit die Bindestriche der UUID nicht vorher in ``_``
   umgeschrieben werden. Der Duplikat-Schutz passiert beim Aufrufer via
   :func:`resolve_uuid_names`: kollidieren nach dem Entfernen zwei Geschwister,
   werden sie per Seriennummer ``_1, _2, …`` unterschieden (die reine Funktion
   kennt keine Geschwister).
1. Mehrfache Leerzeichen → ``_``.
2. Umlaute ersetzen (``ä→ae``, …) — inkl. NFD- und Mojibake-Varianten.
   2b. Restliche lateinische/französische Akzente strippen (``é→e``, ``ç→c``, …).
   2c. Typografische Sonderzeichen normalisieren (En-Dash → ``-``, „/“ entfernen).
3. exFAT-verbotene Zeichen entfernen (``\\ / : * ? " < > |`` plus 0x00-0x1F).
   3b. Klammern entfernen: öffnende ``( [ {`` → ``_``, schließende ``) ] }`` →
       weg (``tabellenaufgabe(loesung)`` → ``tabellenaufgabe_loesung``,
       ``Projekt(2024)`` → ``Projekt_2024``).
4. Awkward-Combos aufräumen (``_+_``, ``-_``, ``_-``, ``_,``, ``,_``, ``__``…).
   4a. Punkt-getrennte deutsche Datumsangaben auf Bindestriche normalisieren
       (``21.12.2021`` → ``21-12-2021``, ``05.01.26`` → ``05-01-26``); ein
       abschließender Kurz-Datumspunkt (``30.10.`` → ``30-10``) fällt nur, wenn
       kein Buchstabe/Ziffer folgt, damit eine echte Endung (``2.5.pdf``) bleibt.
       Auch eine blanke Kurzform ``DD.MM`` (``08.05`` → ``08-05``) wird umgesetzt,
       aber nur bei plausiblem Tag (01-31) / Monat (01-12), sodass Dezimalzahlen
       wie ``19.99`` unberührt bleiben.
   4b. Bindestrich ``-`` → ``_``, außer er steht direkt zwischen zwei Ziffern
       (``2026-06-05`` bleibt, ``Albert-schweitzer`` → ``Albert_Schweitzer``).
5. Casing:
   - Verzeichnis: ``Upper_Snake_Case`` mit zwei Ausnahmen — komplett
     großgeschriebene oder numerische Tokens (Abkürzungen) bleiben
     unverändert, und deutsche Funktionswörter werden nicht am Anfang
     kleingeschrieben.
   - Datei: komplett kleingeschrieben; ein Unterstrich direkt vor der
     Dateiendung wird entfernt (``test_.txt`` → ``test.txt``,
     ``notes_.tar.gz`` → ``notes.tar.gz``, aber ``test_a.txt`` bleibt).
6. Leeres Ergebnis → ``unnamed``.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable


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
# Klammern: öffnende ( [ { werden zu "_", schließende ) ] } fallen weg.
_OPEN_BRACKETS = re.compile(r"[([{]")
_CLOSE_BRACKETS = re.compile(r"[)\]}]")
_MULTI_SPACE = re.compile(r" +")
_MULTI_UNDERSCORE = re.compile(r"_+")

# Kanonische UUID (``8-4-4-4-12`` Hex), case-insensitive. Die Lookarounds
# stellen sicher, dass wir keinen längeren Hex-Lauf anschneiden.
_UUID = re.compile(
    r"(?<![0-9A-Fa-f])"
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])"
)

# Bindestrich ``-`` → ``_``, außer er steht direkt zwischen zwei Ziffern (damit
# Datumsangaben wie ``2026-06-05`` erhalten bleiben). Die Lookarounds sind
# nullbreit, sodass sich benachbarte Ziffern-Bindestriche (z. B. ``1-2-3``)
# nicht gegenseitig verbrauchen.
_HYPHEN_NOT_BETWEEN_DIGITS = re.compile(r"(?<![0-9])-|-(?![0-9])")

# Punkt-getrennte deutsche Datumsangaben auf Bindestriche normalisieren. Erst die
# volle Form ``D.D.YY(YY)``, dann eine abschließende Kurzform ``D.D.`` — deren
# Punkt fällt nur, wenn kein Buchstabe/Ziffer folgt (Lookahead), damit eine echte
# Dateiendung wie ``2.5.pdf`` erhalten bleibt. Die entstehenden Ziffer-Bindestrich-
# Gruppen bleiben von ``_HYPHEN_NOT_BETWEEN_DIGITS`` unberührt.
_DATE_FULL = re.compile(r"([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{2,4})")
_DATE_TRAILING = re.compile(r"([0-9]{1,2})\.([0-9]{1,2})\.(?![0-9A-Za-z])")
# Blanke Kurzform ``DD.MM`` (ohne Jahr, ohne abschließenden Punkt) — nur wenn sie
# wie ein echtes Datum aussieht (Tag 01-31, Monat 01-12) und nicht von weiteren
# Ziffern umgeben ist, damit Dezimalzahlen/Versionen (``19.99``, ``2.5.pdf``,
# ``1234.56``) unberührt bleiben. Läuft NACH den beiden Regeln oben.
_DATE_BARE = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])\.(0[1-9]|1[0-2])(?!\d)")

# Deutsche Funktionswörter, die in Verzeichnisnamen klein bleiben (außer am Anfang).
_EXCEPTIONS = frozenset({
    "und", "oder", "von", "zu", "in", "mit", "auf", "fuer",
    "aus", "der", "die", "das", "dem", "den", "des",
    "am", "im", "zum",
    "bei", "beim", "an", "durch", "als", "zur", "ein", "einem",
    "einer", "eines", "eine", "anhand", "vom", "vor", "um",
})


def contains_uuid(name: str) -> bool:
    """True, wenn ``name`` eine kanonische UUID enthält (NFC-normalisiert)."""
    return bool(_UUID.search(unicodedata.normalize("NFC", name)))


def _core(name: str, *, strip_uuid: bool = True) -> str:
    # NFC-Normalisierung zuerst: Netzwerk-Shares (Synology/SMB) liefern Namen
    # mal als NFD (z. B. ``ö`` = ``o`` + kombinierendes Trema). Ohne diese
    # Zusammensetzung würden die Umlaut-Ersetzungen unten nicht greifen.
    s = unicodedata.normalize("NFC", name)
    # 0) UUIDs entfernen — vor allem anderen, damit die UUID-eigenen Bindestriche
    #    nicht vorher (Schritt 4b) in ``_`` umgeschrieben werden. Etwaige
    #    Rest-Trennzeichen (``report_.pdf``, ``__``) räumen die Schritte weiter
    #    unten auf; führende/abschließende ``_`` trimmen wir am Ende (nur wenn
    #    tatsächlich eine UUID entfernt wurde, um Altverhalten nicht zu ändern).
    uuid_removed = False
    if strip_uuid:
        stripped = _UUID.sub("", s)
        uuid_removed = stripped != s
        s = stripped
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
    # 3b) Klammern entfernen: öffnende ( [ { -> "_", schließende ) ] } -> weg.
    #     Ein dadurch doppelter "_" wird vom Collapse weiter unten eingedampft;
    #     das Weglassen der schließenden Klammer hält Endpositionen sauber
    #     ("Projekt(2024)" -> "Projekt_2024").
    s = _OPEN_BRACKETS.sub("_", s)
    s = _CLOSE_BRACKETS.sub("", s)
    # 4) Awkward-Combos. Bindestrich-neben-Unterstrich ("-_", "_-") wird hier
    #    absichtlich NICHT behandelt: Schritt 4b unten macht aus jedem '-', das
    #    nicht zwischen zwei Ziffern steht, ein '_', und der Collapse dampft das
    #    Ergebnis ein — diese Fälle können also ohnehin nicht überleben.
    s = s.replace("_+_", "+")
    for combo in ("_,", ",_"):
        s = s.replace(combo, "_")
    # 4a) Punkt-Datumsangaben normalisieren (vor 4b, damit die erzeugten
    #     Ziffer-Bindestrich-Gruppen dann erhalten bleiben). Volle Form zuerst,
    #     dann die abschließende Kurzform.
    s = _DATE_FULL.sub(r"\1-\2-\3", s)
    s = _DATE_TRAILING.sub(r"\1-\2", s)
    s = _DATE_BARE.sub(r"\1-\2", s)
    # 4b) Bindestrich → Unterstrich, außer zwischen zwei Ziffern. Vor dem Casing,
    #     damit Wortgrenzen (Verzeichnisse) neu großgeschrieben werden, und vor
    #     dem finalen ``_``-Kollaps, damit ``--`` → ``__`` eingedampft wird.
    s = _HYPHEN_NOT_BETWEEN_DIGITS.sub("_", s)
    s = _MULTI_UNDERSCORE.sub("_", s)
    # Führende/abschließende ``_`` können durch UUID-Entfernung am Rand entstehen
    # (``<uuid>_report`` → ``_report``); nur dann trimmen, sonst Altverhalten.
    if uuid_removed:
        s = s.strip("_")
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


def sanitize_dir_name(name: str, *, strip_uuid: bool = True) -> str:
    s = _core(name, strip_uuid=strip_uuid)
    if not s:
        return "unnamed"
    parts = s.split("_")
    return "_".join(
        _title_case_word(p, is_first=(i == 0)) for i, p in enumerate(parts)
    )


def sanitize_file_name(name: str, *, strip_uuid: bool = True) -> str:
    s = _core(name, strip_uuid=strip_uuid).lower()
    # Ein Unterstrich direkt vor der Dateiendung ist unerwünscht, z. B.
    # ``test_.txt`` → ``test.txt`` und ``notes_.tar.gz`` → ``notes.tar.gz``,
    # während ``test_a.txt`` unverändert bleibt.
    s = s.replace("_.", ".")
    if s.endswith("_"):
        s = s[:-1]
    return s or "unnamed"


def _insert_serial(base: str, n: int, *, is_file: bool) -> str:
    """Hängt eine Seriennummer ``_n`` an — bei Dateien vor die Endung."""
    if is_file and "." in base:
        stem, ext = base.rsplit(".", 1)
        return f"{stem}_{n}.{ext}"
    return f"{base}_{n}"


def resolve_uuid_serials(
    entries: list[tuple[str, bool, str]],
    *,
    is_file: bool,
    reserved: Iterable[str] = frozenset(),
) -> list[str]:
    """Löst UUID-Kollisionen innerhalb einer Charge per Seriennummer auf.

    ``entries`` ist eine Liste ``(stripped_name, had_uuid, sort_key)``.
    UUID-behaftete Einträge, die sich denselben ``stripped_name`` teilen (>=2),
    erhalten Seriennummern ``_1, _2, …`` in aufsteigender ``sort_key``-Reihenfolge;
    alle übrigen Einträge behalten ihren ``stripped_name``.

    Jede Seriennummer wählt die nächste, deren Name noch nicht belegt ist — durch
    einen nicht-serialisierten (z. B. UUID-losen) Geschwistereintrag, eine andere
    Seriennummer oder ``reserved`` (zusätzlich zu meidende Namen, z. B. bereits
    auf der Platte vorhandene Dateien).
    """
    result = [e[0] for e in entries]
    groups: dict[str, list[int]] = defaultdict(list)
    for i, (s, had, _k) in enumerate(entries):
        if had:
            groups[s].append(i)
    serial_idxs = {i for idxs in groups.values() if len(idxs) >= 2 for i in idxs}
    # Namen, die unverändert bleiben (UUID-lose Geschwister, eindeutige Einträge)
    # plus die vom Aufrufer reservierten Namen.
    claimed = {result[i] for i in range(len(result)) if i not in serial_idxs}
    claimed |= set(reserved)
    for s, idxs in groups.items():
        if len(idxs) < 2:
            continue
        n = 0
        for i in sorted(idxs, key=lambda i: entries[i][2]):
            while True:
                n += 1
                cand = _insert_serial(s, n, is_file=is_file)
                if cand not in claimed:
                    break
            result[i] = cand
            claimed.add(cand)
    return result


def resolve_uuid_names(
    names: Iterable[str],
    sanitizer: Callable[..., str],
) -> list[str]:
    """Sanitisiert eine Geschwister-Charge und entfernt UUIDs konfliktbewusst.

    Für jeden Namen wird die UUID entfernt; kollidieren die Ergebnisse zweier
    (oder mehr) UUID-behafteter Einträge derselben Charge, werden diese per
    Seriennummer ``_1, _2, …`` unterschieden (siehe :func:`resolve_uuid_serials`).

    ``sanitizer`` ist :func:`sanitize_file_name` oder :func:`sanitize_dir_name`.
    Die Auflösung ist rein chargen-basiert (nicht Platten-basiert) und damit
    über Läufe hinweg deterministisch — wichtig für den inkrementellen Skip.
    """
    names = list(names)
    entries = [(sanitizer(n, strip_uuid=True), contains_uuid(n), n) for n in names]
    return resolve_uuid_serials(
        entries, is_file=(sanitizer is sanitize_file_name)
    )
