"""Canonicalisation: units, airport identity, reporting periods.

Every function here is deterministic and testable in isolation. Nothing in
this module calls a model - unit conversion and code lookup are exactly
the kind of work that must never be probabilistic.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path

from services.common.config import SETTINGS
from services.common.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------- units ---

# Sources publish kg, metric tonnes, and unqualified "tonnes". Everything is
# stored in kilograms so downstream aggregation is a plain SUM.
_UNIT_TO_KG = {
    "kg": 1.0,
    "kgs": 1.0,
    "kilogram": 1.0,
    "mt": 1000.0,
    "tonne": 1000.0,
    "tonnes": 1000.0,
    "t": 1000.0,
    "ton": 1000.0,
    "metric tonne": 1000.0,
}


def to_kilograms(value: float, unit: str) -> float:
    key = unit.strip().lower().rstrip(".")
    if key not in _UNIT_TO_KG:
        raise ValueError(f"unknown mass unit: {unit!r}")
    return value * _UNIT_TO_KG[key]


_NUM_RE = re.compile(r"-?[\d,]*\.?\d+")


def parse_number(raw: str | None) -> float | None:
    """Parse a table cell. Returns None for the dash sources use as null."""
    if raw is None:
        return None
    text = str(raw).strip().replace("\n", "").replace(" ", "")
    if text in {"", "-", "--", "NA", "N/A", "nil", "Nil"}:
        return None
    m = _NUM_RE.search(text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def parse_percent(raw: str | None) -> float | None:
    v = parse_number(raw)
    return None if v is None else v


# -------------------------------------------------------------- periods ---

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}
_MONTHS.update({m[:3].lower(): i for m, i in list(_MONTHS.items())})


def month_to_iso(month_name: str, year: int) -> str:
    """'April', 2026 -> '2026-04'."""
    key = month_name.strip().lower()[:3]
    if key not in _MONTHS:
        raise ValueError(f"unrecognised month: {month_name!r}")
    return f"{year:04d}-{_MONTHS[key]:02d}"


def fiscal_to_calendar_months(fy_label: str) -> tuple[str, str]:
    """Indian fiscal year '2026-2027' -> ('2026-04', '2027-03')."""
    m = re.match(r"(\d{4})\s*[-/]\s*(\d{2,4})", fy_label.strip())
    if not m:
        raise ValueError(f"unrecognised fiscal year: {fy_label!r}")
    start = int(m.group(1))
    return f"{start}-04", f"{start + 1}-03"


# ------------------------------------------------------------- airports ---

def strip_non_latin(text: str) -> str:
    """Drop Devanagari from AAI's bilingual cells, keep the Latin name.

    AAI writes airports as 'अमृतसर AMRITSAR' in one cell, and sometimes as
    '(दिल्ली) DELHI' - where removing the Devanagari leaves an empty '( )'
    behind. Left in place that residue makes 'DELHI' unresolvable, so the
    empty brackets are cleaned up here rather than in every caller.
    """
    kept = [ch for ch in text if not unicodedata.name(ch, "").startswith("DEVANAGARI")]
    out = "".join(kept)
    out = re.sub(r"\(\s*\)", " ", out)          # drop emptied brackets
    out = re.sub(r"[^\w\s()/&.-]", " ", out)     # drop stray punctuation
    return re.sub(r"\s+", " ", out).strip()


def name_variants(name: str) -> list[str]:
    """Candidate spellings for one airport cell, most specific first.

    AAI writes 'ADAMPUR (JALANDHAR)' and 'BENGALURU (HAL)': the bracket
    holds either the city the airport serves or which of a city's several
    airports it is. Both halves are worth trying before giving up.
    """
    base = strip_non_latin(name).upper().strip()
    if not base:
        return []
    # Older releases write "HYDERABAD(BEGUMPET)" where newer ones write
    # "HYDERABAD (BEGUMPET)". Without normalising the space the curated
    # alias misses and the row falls back to the bare city name, which
    # merges two genuinely different airports onto one code.
    base = re.sub(r"\s*\(\s*", " (", base)
    base = re.sub(r"\s*\)", ")", base)
    variants = [base]

    # Some releases cut the airport cell short, leaving the bracket open:
    # "ADAMPUR (JALANDH", "HOLLONGI (DONYI P". The text before the bracket
    # is still the airport, so it is worth trying rather than quarantining
    # a row over a truncated label.
    if base.count("(") > base.count(")"):
        head = base.split("(", 1)[0].strip()
        if head:
            variants.append(head)

    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", base)
    if m:
        outer, inner = m.group(1).strip(), m.group(2).strip()
        variants += [v for v in (outer, inner) if v]
    stripped = re.sub(r"\b(AIRPORT|INTERNATIONAL|INTL|CIVIL|ENCLAVE)\b", "", base)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped and stripped not in variants:
        variants.append(stripped)
    return variants


class AirportResolver:
    """Resolves free-text airport names to IATA/ICAO codes.

    Exact match first, then a curated alias table, then fuzzy match with a
    similarity floor. Anything below the floor is refused rather than
    guessed: a wrong airport silently corrupts every downstream ranking,
    so an unresolved row belongs in a quarantine queue.
    """

    def __init__(
        self,
        crosswalk_path: Path | None = None,
        alias_path: Path | None = None,
    ) -> None:
        self.path = Path(crosswalk_path or SETTINGS.seeds_dir / "airports.csv")
        self.alias_path = Path(alias_path or SETTINGS.seeds_dir / "airport_aliases.csv")
        self._by_name: dict[str, dict] = {}
        self._by_iata: dict[str, dict] = {}
        self._by_icao: dict[str, dict] = {}
        self._loaded = False

    def load(self) -> int:
        if self._loaded:
            return len(self._by_name)
        if not self.path.exists():
            log.warning(f"airport crosswalk missing at {self.path}")
            self._loaded = True
            return 0
        with self.path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("airport_name") or "").strip()
                city = (row.get("city") or "").strip()
                if row.get("iata"):
                    self._by_iata[row["iata"].upper()] = row
                if row.get("icao"):
                    self._by_icao[row["icao"].upper()] = row
                for key in {name.upper(), city.upper()}:
                    if key:
                        self._index_name(key, row)
                # 'Indira Gandhi International Airport' -> also index 'INDIRA GANDHI'
                trimmed = re.sub(r"\b(international|airport|intl)\b", "", name, flags=re.I)
                trimmed = re.sub(r"\s+", " ", trimmed).strip().upper()
                if trimmed:
                    self._index_name(trimmed, row)

        aliases = self._load_aliases()
        self._loaded = True
        log.info(
            f"airport crosswalk loaded: {len(self._by_name)} keys, "
            f"{len(self._by_iata)} IATA, {aliases} curated alias(es)"
        )
        return len(self._by_name)

    @staticmethod
    def _primacy(row: dict) -> int:
        """Rank candidates for a city name. Higher wins.

        A city can hold several airports and the bulk file is not ordered
        helpfully. For 'DELHI' it lists Safdarjung (no IATA) before Indira
        Gandhi (DEL); for 'HYDERABAD' it lists Begumpet (BPM, the old
        airport) before Rajiv Gandhi (HYD, where the cargo actually goes).
        First-write-wins would attribute a hub's tonnage to a general
        aviation strip, so candidates are ranked instead: having an IATA
        code counts for more than the name, and 'International' breaks the
        remaining ties. This resolves all five ambiguous Indian cities
        without hard-coding any of them.
        """
        has_iata = bool((row.get("iata") or "").strip())
        is_intl = "international" in (row.get("airport_name") or "").lower()
        return has_iata * 2 + is_intl

    def _index_name(self, key: str, row: dict) -> None:
        existing = self._by_name.get(key)
        if existing is None or self._primacy(row) > self._primacy(existing):
            self._by_name[key] = row

    def _load_aliases(self) -> int:
        """Overlay curated aliases on top of the bulk crosswalk.

        Needed because the bulk reference is frozen around 2017 and misses
        two whole classes of Indian airport: cities renamed since (Kochi
        was Cochin, Mysuru was Mysore) and airports opened under the UDAN
        regional scheme. These entries OVERRIDE the bulk table rather than
        deferring to it, because they are hand-checked and it is stale.

        Rows marked `needs_verification` carry a name and ICAO but a blank
        IATA on purpose: knowing which airport a row refers to is useful,
        inventing its code is not.
        """
        if not self.alias_path.exists():
            return 0
        count = 0
        with self.alias_path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                alias = (row.get("alias") or "").strip().upper()
                if not alias:
                    continue
                record = {
                    "airport_name": row.get("airport_name", ""),
                    "city": row.get("city", ""),
                    "country": row.get("country", ""),
                    "iata": (row.get("iata") or "").strip(),
                    "icao": (row.get("icao") or "").strip(),
                    "alias_confidence": row.get("confidence", ""),
                }
                self._by_name[alias] = record        # curated wins
                if record["iata"]:
                    self._by_iata.setdefault(record["iata"].upper(), record)
                if record["icao"]:
                    self._by_icao.setdefault(record["icao"].upper(), record)
                count += 1
        return count

    def resolve_icao(self, icao: str) -> dict | None:
        """Look up by ICAO. Eurostat keys airports as 'NL_EHAM', so the
        code is already authoritative and no name matching is needed."""
        self.load()
        return self._by_icao.get((icao or "").upper().strip())

    # Bounded on purpose: a resolver lives for one pipeline run and the
    # same airport name recurs on every page, so caching is the whole
    # reason resolution is not the bottleneck.
    @lru_cache(maxsize=4096)  # noqa: B019
    def resolve(self, raw_name: str, country_hint: str | None = None) -> tuple[dict | None, float, str]:
        """Return (record, confidence, method)."""
        self.load()
        variants = name_variants(raw_name)
        if not variants:
            return None, 0.0, "empty"

        # Order matters here. A curated alias is hand-verified; a bare
        # three-letter string matching some airport's IATA code is a
        # coincidence, and the two collide often enough to matter.
        # "GOA" is Indian Goa in this data and Genoa's IATA code
        # everywhere else, so checking the code first sent a whole
        # airport's cargo to Italy.
        for i, name in enumerate(variants):
            if name in self._by_name:
                rec = self._by_name[name]
                if country_hint and (rec.get("country") or "").upper() != country_hint.upper():
                    return rec, 0.75, "exact-name-country-mismatch"
                return rec, 1.0, "exact-name" if i == 0 else f"exact-variant:{name}"

        # Only now try the bare code, and only when it does not contradict
        # the country we were told to expect.
        for name in variants:
            if len(name) == 3 and name in self._by_iata:
                rec = self._by_iata[name]
                if country_hint and (rec.get("country") or "").upper() != country_hint.upper():
                    continue
                return rec, 1.0, "iata"

        name = variants[0]
        pool = list(self._by_name)
        if country_hint:
            pool = [k for k in pool if (self._by_name[k].get("country") or "").upper()
                    == country_hint.upper()] or pool
        close = get_close_matches(name, pool, n=1, cutoff=0.86)
        if close:
            return self._by_name[close[0]], 0.86, f"fuzzy:{close[0]}"
        return None, 0.0, "unresolved"
