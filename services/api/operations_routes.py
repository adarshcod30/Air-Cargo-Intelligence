"""Operating efficiency and market structure.

The cargo endpoints answer how much freight moved. These answer how well it
was moved, how concentrated the market is, and which airports actually moved
the national number - questions the tonnage table cannot address on its own.

Everything reads through views, so the serving role still holds no rights on
any base table.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from services.analytics.operations import (
    attribute_growth,
    describe_correlation,
    herfindahl,
    pearson,
)
from services.api.deps import get_session

router = APIRouter(prefix="/api/v1/operations", tags=["operations"])


@router.get("/efficiency")
def efficiency(
    entity: str | None = Query(None, description="carrier name"),
    direction: str = Query("TOTAL", pattern="^(INTERNATIONAL|DOMESTIC|TOTAL)$"),
    limit: int = Query(60, ge=1, le=400),
    session: Session = Depends(get_session),
) -> dict:
    """Cargo load factor and companion ratios, newest first.

    Load factor is freight tonne-kilometres over available tonne-kilometres.
    A dedicated freighter and a passenger carrier's belly hold sit at
    opposite ends of it, which is the distinction tonnage alone hides.
    """
    where = ["cargo_load_factor_pct IS NOT NULL", "direction = :dir"]
    params: dict = {"dir": direction, "lim": limit}
    if entity:
        where.append("entity_key = :entity")
        params["entity"] = entity
    clause = "WHERE " + " AND ".join(where)

    rows = session.execute(text(f"""
        SELECT entity_key, period, direction,
               cargo_load_factor_pct, tonnes_per_departure, mail_share_pct,
               kg_freight_per_pax, ftk_million, atk_million,
               freight_tonnes, departures, pax_carried
        FROM v_cargo_efficiency {clause}
        ORDER BY sort_key DESC, cargo_load_factor_pct DESC
        LIMIT :lim
    """), params).mappings().all()
    return {"rows": [dict(r) for r in rows], "row_count": len(rows)}


@router.get("/attribution")
def attribution(
    direction: str = Query("TOTAL", pattern="^(INTERNATIONAL|DOMESTIC|TOTAL)$"),
    top: int = Query(12, ge=3, le=40),
    session: Session = Depends(get_session),
) -> dict:
    """Which airports moved the national number, in points of national growth.

    The published statistics carry no commodity breakdown, so "what is
    driving growth" cannot be answered in terms of goods. It can be answered
    exactly in terms of where, and the contributions sum to the national
    change rather than merely ranking movers.
    """
    latest = session.execute(text("""
        SELECT period, sort_key FROM v_cargo_fact
        WHERE grain = 'AIRPORT' AND direction = :dir AND period ~ '^[0-9]{4}-[0-9]{2}$'
        ORDER BY sort_key DESC LIMIT 1
    """), {"dir": direction}).mappings().first()
    if not latest:
        return {"detail": "no monthly airport data held"}

    now_p = latest["period"]
    year, month = now_p.split("-")
    then_p = f"{int(year) - 1}-{month}"

    def series(period: str) -> tuple[dict, dict]:
        rows = session.execute(text("""
            SELECT airport_iata AS k, airport_name AS name,
                   SUM(tonnage_kg) / 1000.0 AS mt
            FROM v_cargo_fact
            WHERE grain = 'AIRPORT' AND direction = :dir AND period = :p
              AND airport_iata IS NOT NULL
            GROUP BY airport_iata, airport_name
        """), {"dir": direction, "p": period}).mappings().all()
        return ({r["k"]: float(r["mt"]) for r in rows},
                {r["k"]: r["name"] for r in rows})

    now, names = series(now_p)
    then, names_then = series(then_p)
    names = {**names_then, **names}
    if not then:
        return {"detail": f"no comparable period {then_p} to measure against"}

    result = attribute_growth(now, then, names, top=top)
    if result is None:
        return {"detail": "no base to attribute against"}
    payload = result.to_dict()
    payload["period_now"] = now_p
    payload["period_then"] = then_p
    payload["direction"] = direction
    return payload


@router.get("/concentration")
def concentration(
    direction: str = Query("TOTAL", pattern="^(INTERNATIONAL|DOMESTIC|TOTAL)$"),
    session: Session = Depends(get_session),
) -> dict:
    """How concentrated airport freight is, over recent periods.

    Reported with the effective number of equally sized competitors, since
    an HHI on its own communicates nothing to a general reader.
    """
    periods = session.execute(text("""
        SELECT DISTINCT period, sort_key FROM v_cargo_fact
        WHERE grain = 'AIRPORT' AND direction = :dir AND period ~ '^[0-9]{4}-[0-9]{2}$'
        ORDER BY sort_key DESC LIMIT 12
    """), {"dir": direction}).mappings().all()

    out = []
    for p in reversed(periods):
        vals = session.execute(text("""
            SELECT SUM(tonnage_kg) / 1000.0 AS mt FROM v_cargo_fact
            WHERE grain = 'AIRPORT' AND direction = :dir AND period = :p
              AND airport_iata IS NOT NULL
            GROUP BY airport_iata
        """), {"dir": direction, "p": p["period"]}).scalars().all()
        h = herfindahl([float(v) for v in vals])
        h["period"] = p["period"]
        h["airports"] = len(vals)
        out.append(h)
    return {"rows": out, "row_count": len(out), "direction": direction}


@router.get("/belly-dependency")
def belly_dependency(session: Session = Depends(get_session)) -> dict:
    """Does a carrier's freight move with its passenger flying?

    A strong positive relationship means freight rides in the belly of
    passenger aircraft and rises and falls with the passenger schedule. A
    dedicated freight operator shows no such relationship, because its
    cargo does not depend on passengers being carried at all.
    """
    # Carriers with no passenger operation are kept, not filtered out.
    # Requiring pax_carried > 0 to make the correlation computable excluded
    # exactly the dedicated freighters the question is about, so the panel
    # could only ever answer "belly". Having no passengers to correlate
    # against is the finding, not missing data.
    rows = session.execute(text("""
        SELECT entity_key, period, freight_tonnes, pax_carried,
               cargo_load_factor_pct, tonnes_per_departure
        FROM v_cargo_efficiency
        WHERE direction = 'TOTAL' AND freight_tonnes IS NOT NULL
        ORDER BY entity_key, sort_key
    """)).mappings().all()

    by_entity: dict[str, list] = {}
    for r in rows:
        by_entity.setdefault(r["entity_key"], []).append(r)

    out = []
    for key, series in by_entity.items():
        pax = [float(x["pax_carried"]) for x in series if x["pax_carried"] is not None]
        carries_pax = any(v > 0 for v in pax)

        r = None
        if carries_pax:
            paired = [x for x in series
                      if x["pax_carried"] is not None and float(x["pax_carried"]) > 0]
            r = pearson([float(x["freight_tonnes"]) for x in paired],
                        [float(x["pax_carried"]) for x in paired])

        lfs = [float(x["cargo_load_factor_pct"]) for x in series
               if x["cargo_load_factor_pct"] is not None]
        tpds = [float(x["tonnes_per_departure"]) for x in series
                if x["tonnes_per_departure"] is not None]
        mean_tpd = round(sum(tpds) / len(tpds), 2) if tpds else None

        # Two independent signals agree on what kind of operation this is:
        # a carrier with no passengers at all, and one lifting many tonnes
        # per departure. Either alone is suggestive; together they are the
        # difference between a freighter and a belly-hold seller.
        if not carries_pax:
            kind, reading = "freighter", "carries no passengers — freight is the whole operation"
        elif mean_tpd is not None and mean_tpd >= 5:
            kind, reading = "freighter", f"lifts {mean_tpd} tonnes per departure — freighter aircraft"
        else:
            kind, reading = "belly", describe_correlation(r)

        out.append({
            "entity_key": key,
            "months": len(series),
            "kind": kind,
            "correlation": None if r is None else round(r, 3),
            "reading": reading,
            "mean_load_factor_pct": round(sum(lfs) / len(lfs), 1) if lfs else None,
            "mean_tonnes_per_departure": mean_tpd,
        })
    # Freighters first: they are the minority and the more informative case.
    out.sort(key=lambda x: (x["kind"] != "freighter", -(x["mean_tonnes_per_departure"] or 0)))
    return {"rows": out, "row_count": len(out)}


@router.get("/metrics")
def metric_catalogue(session: Session = Depends(get_session)) -> dict:
    """What operating quantities are held, and how much of each."""
    rows = session.execute(text("""
        SELECT metric, unit, count(*) AS observations,
               count(DISTINCT entity_key) AS entities,
               min(period) AS first_period, max(period) AS last_period
        FROM v_operating_metric
        GROUP BY metric, unit ORDER BY observations DESC
    """)).mappings().all()
    return {"rows": [dict(r) for r in rows], "row_count": len(rows)}
