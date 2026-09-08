"""Source registry: what we ingest, how to reach it, and its real state.

`status` is recorded honestly rather than aspirationally, because the
orchestrator uses it to decide what to attempt and the operator uses it to
know what is actually covered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from services.common.models import Publisher


class SourceStatus(str, Enum):
    ACTIVE = "ACTIVE"                    # verified reachable and parsed
    NEEDS_CREDENTIAL = "NEEDS_CREDENTIAL"
    NEEDS_DISCOVERY = "NEEDS_DISCOVERY"  # reachable, no stable link pattern
    DEGRADED = "DEGRADED"                # intermittent from our network


@dataclass
class Source:
    key: str
    publisher: Publisher
    title: str
    status: SourceStatus
    index_url: str | None = None
    link_patterns: list[str] = field(default_factory=list)
    # Template for constructing URLs for periods the index no longer
    # links. Placeholders: {mon} {month} {yy} {yyyy}.
    archive_template: str | None = None
    archive_months_back: int = 0
    api_template: str | None = None
    # Cartesian product of these fills api_template, one document per combo.
    api_params: dict[str, list[str]] = field(default_factory=dict)
    notes: str = ""


REGISTRY: list[Source] = [
    Source(
        key="aai_freight",
        publisher=Publisher.AAI,
        title="AAI monthly traffic news - Annexure IV (freight, MT)",
        status=SourceStatus.ACTIVE,
        index_url="https://www.aai.aero/en/business-opportunities/aai-traffic-news",
        # Both spellings on purpose: AAI ships 'Anex5' alongside 'Annex4'.
        link_patterns=[r"an+ex\s*4", r"an+ex4"],
        # The traffic-news page lists only the most recent months, but the
        # files themselves stay on the server: Jan 2023 is still fetchable
        # long after the page stopped linking it. Without this the whole
        # archive is invisible and every airport series is ~7 points long,
        # too short to decompose seasonally or to forecast.
        archive_template=(
            "https://www.aai.aero/sites/default/files/traffic-news/"
            "{mon}2k{yy}Annex4.pdf"
        ),
        archive_months_back=44,
        notes="Annexure IV-A international, IV-B domestic, IV-C total. Values in MT.",
    ),
    Source(
        key="eurostat_avia_gooa",
        publisher=Publisher.EUROSTAT,
        title="Eurostat avia_gooa - freight and mail by main airport",
        status=SourceStatus.ACTIVE,
        api_template=(
            "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
            "avia_gooa?format=JSON&lang=EN&rep_airp={airport}&time={year}"
        ),
        api_params={
            # Europe's largest freight airports. Eurostat refuses unfiltered
            # queries with HTTP 413, so the airport list is explicit.
            "airport": [
                "DE_EDDF",  # Frankfurt
                "NL_EHAM",  # Amsterdam Schiphol
                "FR_LFPG",  # Paris Charles de Gaulle
                "BE_EBLG",  # Liege
                "LU_ELLX",  # Luxembourg
                "DE_EDDL",  # Dusseldorf
                "ES_LEMD",  # Madrid Barajas
                "IT_LIMC",  # Milan Malpensa
            ],
            "year": ["2022", "2023"],
        },
        notes="JSON-stat. Requires a rep_airp filter; unfiltered queries return 413.",
    ),
    Source(
        key="openflights_airports",
        publisher=Publisher.OPENFLIGHTS,
        title="OpenFlights airport crosswalk (IATA/ICAO reference)",
        status=SourceStatus.ACTIVE,
        index_url=(
            "https://raw.githubusercontent.com/jpatokal/openflights/master/"
            "data/airports.dat"
        ),
        notes="Reference data for airport resolution, not a cargo source.",
    ),
    Source(
        key="dgca_traffic",
        publisher=Publisher.DGCA,
        title="DGCA traffic statistics",
        status=SourceStatus.NEEDS_DISCOVERY,
        index_url="https://www.dgca.gov.in/digigov-portal/",
        link_patterns=[r"traffic", r"statist", r"cargo", r"freight"],
        notes=(
            "Portal renders report links via JavaScript, so no static hrefs are "
            "exposed. Needs either a rendered crawl or a manually seeded path."
        ),
    ),
    Source(
        key="data_gov_in_cargo",
        publisher=Publisher.DATA_GOV_IN,
        title="Open Government Data portal - air cargo datasets",
        status=SourceStatus.NEEDS_CREDENTIAL,
        api_template="https://api.data.gov.in/resource/{resource_id}?api-key={key}&format=json",
        notes=(
            "Catalogue-driven: api.data.gov.in/lists is paged to discover every "
            "air-cargo resource, then each is pulled by its resource_id. ONE key "
            "covers the whole platform - verified against the live API - so no "
            "dataset needs downloading by hand. Set DATA_GOV_IN_API_KEY to enable."
        ),
    ),
    Source(
        key="world_bank_air_freight",
        publisher=Publisher.WORLD_BANK,
        title="World Bank air transport freight (IS.AIR.GOOD.MT.K1)",
        status=SourceStatus.DEGRADED,
        api_template=(
            "https://api.worldbank.org/v2/country/{countries}/indicator/"
            "IS.AIR.GOOD.MT.K1?format=json&per_page=500"
        ),
        notes="Endpoint timed out repeatedly from our network. Retried with backoff.",
    ),
]


def get(key: str) -> Source | None:
    return next((s for s in REGISTRY if s.key == key), None)


def is_runnable(source: Source) -> bool:
    """Whether this source can actually run right now.

    NEEDS_CREDENTIAL is a statement about configuration, not about the
    source itself. Once the credential is present the source is as
    runnable as any other, so the status is resolved at call time rather
    than being baked in - otherwise supplying a key silently changes
    nothing and `--all` keeps skipping it.
    """
    if source.status is SourceStatus.ACTIVE:
        return True
    if source.status is SourceStatus.NEEDS_CREDENTIAL:
        return bool(_credential_for(source))
    return False


def _credential_for(source: Source) -> str | None:
    from services.common.config import SETTINGS

    return {
        "data_gov_in_cargo": SETTINGS.data_gov_in_api_key,
    }.get(source.key)


def active() -> list[Source]:
    """Sources that can run now, credentials included."""
    return [s for s in REGISTRY if is_runnable(s)]
