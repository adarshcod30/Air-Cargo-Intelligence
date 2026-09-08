"""Catalogue client for the Open Government Data platform (data.gov.in).

Two facts drive this module's shape, both verified against the live
platform rather than assumed:

1. **One API key covers every dataset.** A user gets a single key; each
   dataset is addressed by its own `resource_id` (a UUID). So there is no
   need to download datasets by hand - the whole aviation catalogue can be
   pulled programmatically once the key is set.

2. **The catalogue itself is an API.** `api.data.gov.in/lists` paginates
   the full resource index, and it accepts the same key. That is how the
   aviation subset is discovered instead of being hard-coded.

`/lists` does support a server-side sector filter, in the undocumented
form `filters[sector]=Aviation`. It is case-sensitive: `Aviation` returns
373 records and matches the portal exactly, while `aviation` returns
zero. Using it turns a 287,810-record crawl into four requests, so the
filter is applied server-side and the local classifier then narrows
aviation down to air-cargo specifically.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from services.common.config import SETTINGS
from services.common.logging import get_logger

log = get_logger(__name__)

LISTS_URL = "https://api.data.gov.in/lists"
RESOURCE_URL = "https://api.data.gov.in/resource/{resource_id}"

# Words that mark a dataset as plausibly about air cargo. Matched against
# title, description and field names.
CARGO_TERMS = (
    "cargo", "freight", "tonnage", "tonne", "goods", "exim",
    "export", "import", "consignment",
)
AVIATION_TERMS = (
    "aviation", "airport", "airline", "aircraft", "air ", "flight",
    "dgca", "aai", "civil aviation",
)


class MissingApiKey(RuntimeError):
    """Raised when the OGD key is absent, with the exact fix."""

    def __init__(self) -> None:
        super().__init__(
            "DATA_GOV_IN_API_KEY is not set. Register free at "
            "https://www.data.gov.in/user/register, copy the key from your "
            "profile, then add it to .env as DATA_GOV_IN_API_KEY=<key>. "
            "One key covers every dataset on the platform."
        )


@dataclass
class OgdResource:
    """One catalogue entry."""

    resource_id: str
    title: str
    description: str = ""
    org: str = ""
    sector: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)

    @property
    def haystack(self) -> str:
        return " ".join(
            [self.title, self.description, " ".join(self.sector), " ".join(self.fields)]
        ).lower()

    def relevance(self) -> tuple[bool, list[str]]:
        """Is this an air-cargo dataset, and on what evidence?

        Both halves must match. 'Freight' alone catches railway goods
        traffic; 'airport' alone catches passenger and movement tables.
        Returning the matched terms keeps the decision auditable instead
        of leaving a bare boolean.
        """
        h = self.haystack
        cargo_hits = [t for t in CARGO_TERMS if t in h]
        aviation_hits = [t for t in AVIATION_TERMS if t in h]
        return bool(cargo_hits and aviation_hits), cargo_hits + aviation_hits

    def api_url(self, api_key: str, limit: int = 1000, offset: int = 0) -> str:
        return (
            f"{RESOURCE_URL.format(resource_id=self.resource_id)}"
            f"?api-key={api_key}&format=json&limit={limit}&offset={offset}"
        )

    def to_dict(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "title": self.title,
            "org": self.org,
            "sector": self.sector,
            "fields": self.fields,
        }


class OgdCatalogue:
    """Pages `api.data.gov.in/lists` and caches the result."""

    def __init__(self, api_key: str | None = None, cache_path: Path | None = None) -> None:
        self.api_key = api_key or SETTINGS.data_gov_in_api_key
        self.cache_path = Path(
            cache_path or SETTINGS.interim_dir / "ogd_catalogue.json"
        )

    # ------------------------------------------------------------------ #

    def fetch_index(
        self,
        limit: int = 100,
        max_records: int = 250_000,
        sector: str | None = "Aviation",
    ) -> list[OgdResource]:
        """Download the resource index, page by page.

        `sector` is passed straight to the platform's own filter. It is
        case-sensitive there, so it is sent verbatim.
        """
        if not self.api_key:
            raise MissingApiKey()

        resources: list[OgdResource] = []
        offset = 0
        with httpx.Client(timeout=SETTINGS.request_timeout,
                          headers={"User-Agent": SETTINGS.user_agent}) as client:
            while offset < max_records:
                params = {
                    "api-key": self.api_key,
                    "format": "json",
                    "offset": offset,
                    "limit": limit,
                }
                if sector:
                    params["filters[sector]"] = sector
                payload = None
                for attempt in range(1, SETTINGS.max_retries + 1):
                    try:
                        resp = client.get(LISTS_URL, params=params)
                        resp.raise_for_status()
                        payload = resp.json()
                        break
                    except Exception as exc:
                        log.warning(
                            f"catalogue offset {offset} attempt {attempt}: "
                            f"{type(exc).__name__}"
                        )
                        time.sleep(SETTINGS.polite_delay_s * attempt)
                if payload is None:
                    break

                records = payload.get("records") or []
                if not records:
                    break
                for rec in records:
                    resources.append(_to_resource(rec))
                total = payload.get("total")
                offset += limit
                log.info(f"catalogue: {len(resources)}/{total} indexed")
                if total is not None and len(resources) >= int(total):
                    break
                time.sleep(SETTINGS.polite_delay_s)

        log.info(f"catalogue complete: {len(resources)} resources")
        return resources

    # ------------------------------------------------------------------ #

    def load_or_fetch(self, refresh: bool = False) -> list[OgdResource]:
        if self.cache_path.exists() and not refresh:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            log.info(f"catalogue loaded from cache: {len(raw)} resources")
            return [OgdResource(**r) for r in raw]
        resources = self.fetch_index()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps([r.to_dict() for r in resources], indent=1), encoding="utf-8"
        )
        return resources

    def air_cargo_resources(self, refresh: bool = False) -> list[tuple[OgdResource, list[str]]]:
        """Every catalogue entry that looks like air-cargo data, with evidence."""
        out = []
        for r in self.load_or_fetch(refresh=refresh):
            ok, evidence = r.relevance()
            if ok:
                out.append((r, evidence))
        log.info(f"air-cargo candidates: {len(out)}")
        return out


def _to_resource(rec: dict) -> OgdResource:
    """Map one `/lists` record onto our shape.

    The platform is not consistent about key names between record types,
    so each value is looked up across the spellings actually observed.
    """

    def pick(*keys: str, default=""):
        for k in keys:
            v = rec.get(k)
            if v:
                return v
        return default

    sector = pick("sector", "field_sector", default=[])
    if isinstance(sector, str):
        sector = [sector]

    fields = []
    for f in rec.get("field") or []:
        if isinstance(f, dict):
            fields.append(str(f.get("name") or f.get("id") or ""))
        else:
            fields.append(str(f))

    return OgdResource(
        resource_id=str(pick("index_name", "resource_id", "id")),
        title=str(pick("title", "name")),
        description=str(pick("desc", "description")),
        org=str(pick("org_type", "org", "source")),
        sector=[str(s) for s in sector],
        fields=fields,
    )
