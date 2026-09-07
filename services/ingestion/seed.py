"""Build the airport crosswalk that reconciliation depends on.

Reconciliation cannot resolve 'AMRITSAR' to a code without a reference
table, so this runs before the first ingest. Source is OpenFlights, which
publishes IATA, ICAO, city and country for ~7,700 airports.
"""

from __future__ import annotations

import csv
from pathlib import Path

from services.common.config import SETTINGS
from services.common.logging import get_logger
from services.ingestion.fetcher import fetch
from services.ingestion.registry import get as get_source

log = get_logger(__name__)

_COLUMNS = ["airport_id", "airport_name", "city", "country", "iata", "icao",
            "latitude", "longitude", "timezone"]


def build_airport_crosswalk(dest: Path | None = None) -> int:
    """Fetch OpenFlights and write db/seeds/airports.csv."""
    source = get_source("openflights_airports")
    assert source and source.index_url
    dest = Path(dest or SETTINGS.seeds_dir / "airports.csv")

    res = fetch(source.index_url)
    if not res.ok:
        log.error(f"crosswalk fetch failed: HTTP {res.status}")
        return 0

    text = res.payload.decode("utf-8", "ignore")
    rows: list[dict] = []
    for parts in csv.reader(text.splitlines()):
        # OpenFlights columns: id,name,city,country,iata,icao,lat,lon,alt,tz,...
        if len(parts) < 12:
            continue
        iata = parts[4].strip().strip('"')
        icao = parts[5].strip().strip('"')
        rows.append({
            "airport_id": parts[0],
            "airport_name": parts[1],
            "city": parts[2],
            "country": parts[3],
            "iata": "" if iata in {"", "\\N"} else iata,
            "icao": "" if icao in {"", "\\N"} else icao,
            "latitude": parts[6],
            "longitude": parts[7],
            "timezone": parts[11],
        })

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    indian = sum(1 for r in rows if r["country"] == "India")
    log.info(f"airport crosswalk written: {len(rows)} airports ({indian} Indian) -> {dest}")
    return len(rows)


if __name__ == "__main__":
    build_airport_crosswalk()
