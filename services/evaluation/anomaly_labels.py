"""Ground truth for the alert feed, and the precision it earns.

Precision and recall need labels, and labels need a source of truth the
detector does not already read. That is the whole difficulty here: the
detector reads the tonnage series, so a label derived from the same series
risks scoring the detector against itself.

Two proxies were tried and rejected on evidence rather than taste:

  The publisher's own year-on-year change. Rejected: it separates flagged
  from unflagged months barely at all - 33% of flagged and 27% of unflagged
  exceed a 50% swing. Year-on-year movement and departure from a seasonal
  pattern are different questions, and a small volatile airport answers the
  first loudly every month without anything happening.

  Persistence of the level shift. Rejected: it marked 80% of unflagged
  months as genuine events, which is not a credible base rate and means the
  rule was firing on ordinary variation.

What survives is a narrow rule that only labels cases nobody would argue
about, and a table that a person can extend. A precision figure computed
here reports how many labels it rests on and how many came from a human,
because a number without that context is not evidence.

    python -m services.evaluation.anomaly_labels --seed
    python -m services.evaluation.anomaly_labels --report
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.common.logging import get_logger
from services.warehouse.loader import get_engine

log = get_logger(__name__)

# Months either side used to decide that a service genuinely started or
# stopped rather than reporting a gap.
WINDOW = 3


def seed(session: Session, replace: bool = False) -> dict:
    """Insert the labels that follow from the data without judgement.

    Two classes qualify:

      A service starting or stopping. Three zero months followed by three
      non-zero ones is not a statistical curiosity; something began. The
      reverse is something ending. Both are events an operations team wants
      named, and neither depends on a threshold.

      A row whose published components do not sum. INTERNATIONAL plus
      DOMESTIC must equal TOTAL; where it does not by more than 5%, the
      defect is in the source or the parse, and any movement it produces is
      not a cargo event.
    """
    from services.warehouse.queries import load_series

    if replace:
        session.execute(text("DELETE FROM anomaly_label WHERE source = 'rule'"))
        session.commit()

    period_ids = {
        r[0]: r[1] for r in session.execute(
            text("SELECT period_label, period_id FROM dim_period"))
    }

    broken = {
        (r[0], r[1]) for r in session.execute(text("""
            WITH d AS (
              SELECT COALESCE(ap.iata_code, ap.airport_name) AS ek,
                     p.period_label AS per,
                     SUM(f.tonnage_kg) FILTER (WHERE f.direction='INTERNATIONAL') i,
                     SUM(f.tonnage_kg) FILTER (WHERE f.direction='DOMESTIC') d,
                     SUM(f.tonnage_kg) FILTER (WHERE f.direction='TOTAL') t
              FROM fact_cargo_movement f
              JOIN dim_period p ON p.period_id = f.period_id
              LEFT JOIN dim_airport ap ON ap.airport_id = f.airport_id
              WHERE f.grain = 'AIRPORT' GROUP BY 1, 2)
            SELECT ek, per FROM d
            WHERE i IS NOT NULL AND d IS NOT NULL AND t > 0
              AND abs((i + d) - t) / t > 0.05
        """))
    }

    series = [s for s in load_series(session, min_points=3) if len(s) >= 2 * WINDOW + 1]
    rows: list[dict] = []

    for sr in series:
        v = sr.values
        for i, per in enumerate(sr.periods):
            before, after = v[max(0, i - WINDOW):i], v[i + 1:i + 1 + WINDOW]
            if len(before) < WINDOW or len(after) < WINDOW:
                continue
            pid = period_ids.get(per)
            if pid is None:
                continue

            started = all(x == 0 for x in before) and all(x > 0 for x in after) and v[i] > 0
            stopped = all(x > 0 for x in before) and all(x == 0 for x in after)
            if started or stopped:
                rows.append({
                    "grain": sr.grain, "entity_key": sr.entity_key[:64],
                    "period_id": pid, "direction": sr.direction,
                    "label": "GENUINE",
                    "basis": ("service started: three zero months then three "
                              "reporting traffic")
                    if started else
                    ("service stopped: three months reporting traffic then "
                     "three at zero"),
                    "source": "rule",
                })
            elif (sr.entity_key, per) in broken:
                rows.append({
                    "grain": sr.grain, "entity_key": sr.entity_key[:64],
                    "period_id": pid, "direction": sr.direction,
                    "label": "SPURIOUS",
                    "basis": "published components do not sum: "
                             "INTERNATIONAL + DOMESTIC differs from TOTAL by over 5%",
                    "source": "rule",
                })

    from services.warehouse.loader import batched_upsert
    from services.warehouse.schema import AnomalyLabelRow

    # A human label always wins: seeding must never overwrite judgement.
    existing_human = {
        (r[0], r[1], r[2], r[3]) for r in session.execute(text(
            "SELECT grain::text, entity_key, period_id, direction::text "
            "FROM anomaly_label WHERE source = 'human'"))
    }
    rows = [r for r in rows
            if (r["grain"], r["entity_key"], r["period_id"], r["direction"])
            not in existing_human]

    written = batched_upsert(
        session, AnomalyLabelRow, "anomaly_label_natural_key",
        ["label", "basis", "source"], rows,
        lambda r: (r["grain"], r["entity_key"], r["period_id"], r["direction"]),
    )
    session.commit()

    by_label: dict[str, int] = {}
    for r in rows:
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
    summary = {"labels_written": written, "by_label": by_label,
               "human_labels_preserved": len(existing_human)}
    log.info(f"anomaly labels: {summary}")
    return summary


def evaluate(session: Session) -> dict:
    """Precision and recall of the alert feed over the labelled rows only.

    Reported with the label count and how many came from a person, because
    precision over a handful of rule-assigned labels is a weaker claim than
    the same number over a reviewed set, and the reader cannot tell the
    difference from the figure alone.
    """
    # Compared at the granularity the feed reports: one alert per entity and
    # period, whichever direction best explains it. Labels are recorded per
    # direction because that is what the series are, but a service starting
    # shows up in DOMESTIC and TOTAL alike, and the feed deliberately raises
    # it once. Joining on direction scored the same event as caught in one
    # direction and missed in the others - the very over-count that was
    # removed from the feed, reappearing in its evaluation.
    rows = session.execute(text("""
        WITH per_label AS (
            -- Volume of the labelled series itself, in its own direction.
            -- Taking the maximum across directions instead measured the
            -- airport's total, so a small international service starting at
            -- a large airport counted as a material event the detector was
            -- then marked down for suppressing. The evaluation has to use
            -- the bar the detector applies.
            SELECT l.grain, l.entity_key, l.period_id, l.direction,
                   l.label, l.source,
                   COALESCE(MAX(f.tonnage_kg), 0) AS level_kg
            FROM anomaly_label l
            LEFT JOIN fact_cargo_movement f
                   ON f.period_id = l.period_id
                  AND f.direction = l.direction
                  AND f.grain = l.grain
                  AND COALESCE(
                        (SELECT ap.iata_code FROM dim_airport ap
                          WHERE ap.airport_id = f.airport_id),
                        (SELECT ap.airport_name FROM dim_airport ap
                          WHERE ap.airport_id = f.airport_id),
                        (SELECT al.airline_name FROM dim_airline al
                          WHERE al.airline_id = f.airline_id)) = l.entity_key
            GROUP BY 1, 2, 3, 4, 5, 6
        ),
        events AS (
            -- Collapsing directions needs a stated preference, not an
            -- aggregate that happens to sort the right way. MAX(source)
            -- returned 'rule' for any event carrying one rule label,
            -- because 'rule' > 'human' alphabetically - so a reviewed
            -- event would have reported as unreviewed, understating the
            -- human labels the figures rest on. Invisible at zero human
            -- labels, wrong from the first one.
            SELECT grain, entity_key, period_id,
                   CASE WHEN bool_or(label = 'GENUINE')
                        THEN 'GENUINE' ELSE 'SPURIOUS' END AS label,
                   CASE WHEN bool_or(source = 'human')
                        THEN 'human' ELSE 'rule' END AS source,
                   MAX(level_kg)  AS level_kg
            FROM per_label
            GROUP BY 1, 2, 3
        )
        SELECT e.label, e.source, (a.anomaly_id IS NOT NULL) AS flagged,
               e.level_kg
        FROM events e
        LEFT JOIN anomaly a
               ON a.grain = e.grain AND a.entity_key = e.entity_key
              AND a.period_id = e.period_id
    """)).mappings().all()

    if not rows:
        return {"label_rows": 0, "labelled_events": 0,
                "detail": "no labels; run --seed or label by hand"}

    tp = sum(1 for r in rows if r["label"] == "GENUINE" and r["flagged"])
    fp = sum(1 for r in rows if r["label"] == "SPURIOUS" and r["flagged"])
    fn = sum(1 for r in rows if r["label"] == "GENUINE" and not r["flagged"])
    tn = sum(1 for r in rows if r["label"] == "SPURIOUS" and not r["flagged"])

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    human = sum(1 for r in rows if r["source"] == "human")

    # Recall over events the feed is meant to carry. A service handling one
    # tonne a month is a real event and deliberately not alerted; counting
    # it as a miss measures the materiality bar, not the detector. Both
    # figures are reported so neither can be quoted alone.
    from services.analytics.anomaly import STRUCTURAL_MIN_MT

    material = [r for r in rows
                if float(r["level_kg"] or 0) / 1000.0 >= STRUCTURAL_MIN_MT]
    m_tp = sum(1 for r in material if r["label"] == "GENUINE" and r["flagged"])
    m_fn = sum(1 for r in material if r["label"] == "GENUINE" and not r["flagged"])
    material_recall = m_tp / (m_tp + m_fn) if (m_tp + m_fn) else None

    label_rows = session.execute(
        text("SELECT count(*) FROM anomaly_label")).scalar() or 0

    return {
        # Rows and events are different populations: a service starting is
        # labelled in DOMESTIC and TOTAL alike but is one event, and the
        # feed raises it once. Printing only one number invited the reader
        # to compare it against a table count it does not equal.
        "label_rows": label_rows,
        "labelled_events": len(rows),
        "human_labels": human,
        "genuine": tp + fn,
        "spurious": fp + tn,
        "true_positives": tp, "false_positives": fp,
        "false_negatives": fn, "true_negatives": tn,
        "precision": round(precision, 3) if precision is not None else None,
        "recall_all_events": round(recall, 3) if recall is not None else None,
        "recall_material_events": (round(material_recall, 3)
                                   if material_recall is not None else None),
        "material_true_positives": m_tp,
        "material_events": m_tp + m_fn,
        "suppressed_below_bar": (tp + fn) - (m_tp + m_fn),
    }


def pending_review(session: Session, limit: int = 40) -> list[dict]:
    """Alerts with no label yet, largest first.

    Precision needs labelled false positives, and no rule can assert that an
    alert is spurious - only a person looking at it can. This lists what is
    waiting on that judgement, so the gap is a queue rather than a shrug.
    """
    rows = session.execute(text("""
        SELECT a.grain::text AS grain, a.entity_key, a.direction::text AS direction,
               p.period_label AS period, a.method, a.severity,
               ROUND((a.observed_kg / 1000.0)::numeric, 1) AS observed_mt,
               ROUND((a.expected_kg / 1000.0)::numeric, 1) AS expected_mt,
               ROUND(a.deviation_pct::numeric, 1) AS deviation_pct
        FROM anomaly a
        JOIN dim_period p ON p.period_id = a.period_id
        LEFT JOIN anomaly_label l
               ON l.grain = a.grain AND l.entity_key = a.entity_key
              AND l.period_id = a.period_id AND l.direction = a.direction
        WHERE l.label_id IS NULL
        ORDER BY a.observed_kg DESC NULLS LAST
        LIMIT :lim
    """), {"lim": limit}).mappings().all()
    return [dict(r) for r in rows]


def record(session: Session, entity_key: str, period: str, direction: str,
           label: str, basis: str, grain: str = "AIRPORT") -> None:
    """Store one human judgement. Human labels are never overwritten by seeding."""
    if label not in ("GENUINE", "SPURIOUS"):
        raise ValueError("label must be GENUINE or SPURIOUS")
    session.execute(text("""
        INSERT INTO anomaly_label (grain, entity_key, period_id, direction,
                                   label, basis, source)
        SELECT CAST(:grain AS grain_enum), :entity_key, p.period_id,
               CAST(:direction AS direction_enum), :label, :basis, 'human'
        FROM dim_period p WHERE p.period_label = :period
        ON CONFLICT (grain, entity_key, period_id, direction) DO UPDATE
           SET label = EXCLUDED.label, basis = EXCLUDED.basis, source = 'human'
    """), {"grain": grain, "entity_key": entity_key, "period": period,
           "direction": direction, "label": label, "basis": basis})
    session.commit()


def main() -> None:
    ap = argparse.ArgumentParser(description="Alert ground truth")
    ap.add_argument("--seed", action="store_true", help="insert the unarguable labels")
    ap.add_argument("--replace", action="store_true", help="re-seed rule labels")
    ap.add_argument("--report", action="store_true", help="precision and recall")
    ap.add_argument("--pending", type=int, metavar="N",
                    help="list N unlabelled alerts awaiting review")
    ap.add_argument("--label", nargs=5,
                    metavar=("ENTITY", "PERIOD", "DIRECTION", "GENUINE|SPURIOUS", "REASON"),
                    help="record one human judgement")
    args = ap.parse_args()

    with Session(get_engine()) as s:
        if args.label:
            entity, period, direction, label, reason = args.label
            record(s, entity, period, direction, label, reason)
            print(f"recorded {label} for {entity} {period} {direction}")
            return
        if args.pending:
            for r in pending_review(s, args.pending):
                dev = f"{r['deviation_pct']:+.0f}%" if r["deviation_pct"] is not None else "  n/a"
                print(f"  {r['entity_key'][:20]:20s} {r['period']:9s} {r['direction']:14s} "
                      f"{r['method']:16s} observed {r['observed_mt']:>10,.1f} MT  "
                      f"expected {r['expected_mt']:>10,.1f} MT  {dev}")
            return
        if args.seed or args.replace:
            print(json.dumps(seed(s, replace=args.replace), indent=2))
        if args.report or not (args.seed or args.replace):
            print(json.dumps(evaluate(s), indent=2))


if __name__ == "__main__":
    main()
