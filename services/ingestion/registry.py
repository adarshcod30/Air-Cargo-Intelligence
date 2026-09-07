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
        notes="api.data.gov.in returns 403 without a key. Free registration required.",
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


def active() -> list[Source]:
    return [s for s in REGISTRY if s.status is SourceStatus.ACTIVE]
