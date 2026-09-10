"""Where the publisher's own arithmetic contradicts itself.

The AAI annexure prints, for every airport, the month's tonnage and the
fiscal-year-to-date beside it. Within a year the cumulative must advance by
exactly the month. That identity is asserted on the publisher's own page
and is independent of anything the detector reads, which is what makes it
usable as ground truth rather than as another opinion about the series.

Nothing had checked it. Over the archived annexures it finds rows where a
published month cannot be reconciled with the published year-to-date, and
those are alerts no operations team should ever see: the movement they
report was withdrawn by the publisher, not observed.

Two it found, both of which had been raised as alerts:

  Mopa (Goa) April 2023 prints 12,234 MT. May's year-to-date is 28 MT, so
  April was about 12 MT. Wrong by a factor of a thousand.

  Bhubaneswar November 2023 prints 4,004 MT and the October and December
  cumulatives agree with it. Then January's year-to-date falls from 10,763
  to 8,271. A year-to-date cannot decrease; the year was restated down.

    python -m services.evaluation.publisher_consistency
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from services.common.logging import get_logger

log = get_logger(__name__)

MONTHS = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]
FNAME = re.compile(r"^([A-Z][a-z]{2})2k(\d{2})Annex4\.pdf$")
SECTIONS = ["INTERNATIONAL", "DOMESTIC", "TOTAL"]

_NUM = r"(-?[\d,]+(?:\.\d+)?|-)"
_ROW = re.compile(
    r"^(?:\S+\s+)?(?:[^\x00-\x7F][^A-Z]*\s+)?([A-Z][A-Z .()\-/&']{3,40}?)\s+"
    + r"\s+".join([_NUM] * 6) + r"\s*$"
)
# A row is only worth reporting when the gap is real rather than rounding.
TOLERANCE_MT = 1.0
TOLERANCE_FRACTION = 0.005


def _num(s: str) -> float | None:
    if s in ("-", ""):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def read_annexure(path: str) -> dict[tuple[str, str], tuple[float, float]]:
    """{(section, airport): (month, cumulative)}.

    The three sections repeat the airport list in a fixed order, so the
    nth occurrence of a name identifies its section without needing to
    find the headings, which are bilingual and inconsistently spaced.
    """
    import pypdf

    seen: dict[str, int] = {}
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for page in pypdf.PdfReader(path).pages:
        for line in (page.extract_text() or "").splitlines():
            m = _ROW.match(" ".join(line.split()))
            if not m:
                continue
            month, cum = _num(m.group(2)), _num(m.group(5))
            if month is None or cum is None:
                continue
            name = m.group(1).strip()
            i = seen.get(name, 0)
            seen[name] = i + 1
            if i < len(SECTIONS):
                out[(SECTIONS[i], name)] = (month, cum)
    return out


def _fiscal_key(filename: str) -> tuple[int, int] | None:
    m = FNAME.match(filename)
    if not m:
        return None
    mon, yy = m.group(1), int(m.group(2))
    idx = MONTHS.index(mon)
    # January to March belong to the fiscal year that opened the previous April.
    return (2000 + yy - (1 if idx >= 9 else 0), idx)


def contradictions(ledger: str = "data/raw/_ledger.jsonl") -> list[dict]:
    """Rows whose published month cannot be reconciled with the cumulative."""
    docs: dict[tuple[int, int], str] = {}
    for line in Path(ledger).read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        key = _fiscal_key(d.get("source_url", "").rsplit("/", 1)[-1])
        raw = d.get("raw_path")
        if key and raw and Path(raw).exists():
            docs[key] = raw

    parsed = {k: read_annexure(v) for k, v in sorted(docs.items())}
    out: list[dict] = []
    for (fy, mi), rows in sorted(parsed.items()):
        prev = parsed.get((fy, mi - 1)) if mi > 0 else None
        for (section, name), (month, cum) in rows.items():
            if mi == 0:
                expected = month                       # April opens the year
            elif prev and (section, name) in prev:
                expected = prev[(section, name)][1] + month
            else:
                continue
            gap = cum - expected
            if abs(gap) <= max(TOLERANCE_MT, TOLERANCE_FRACTION * max(abs(expected), 1)):
                continue
            # The gap, not an implied month. Attributing it to this month
            # produces impossible values when the error is upstream: Mopa's
            # May would read as -12,206 MT, because the wrong figure is
            # April's. What the check proves is that two published numbers
            # disagree, and printing a derived third invites the reader to
            # believe it knows which.
            out.append({
                "fiscal_year": fy, "month": MONTHS[mi], "section": section,
                "airport": name, "published_month_mt": month,
                "published_cumulative_mt": cum,
                "cumulative_expected_mt": expected,
                "gap_mt": gap,
            })
    out.sort(key=lambda r: -abs(r["gap_mt"]))
    log.info(f"publisher consistency: {len(out)} contradiction(s) over {len(parsed)} annexures")
    return out


def main() -> None:
    rows = contradictions()
    print(f"\n{len(rows)} row(s) where the month contradicts the cumulative\n")
    for r in rows:
        print(f"  FY{r['fiscal_year']} {r['month']:<4} {r['section']:<14} "
              f"{r['airport'][:24]:<24} month {r['published_month_mt']:>9,.0f} MT, "
              f"year-to-date {r['published_cumulative_mt']:>10,.0f} MT "
              f"where {r['cumulative_expected_mt']:>10,.0f} was due "
              f"({r['gap_mt']:+,.0f})")
    print("\nThe check proves the two published figures cannot both be right. "
          "It does not say which,\nso an alert resting on either is not "
          "evidence of a cargo event.")


if __name__ == "__main__":
    main()
