"""Periodic cargo briefs (SRS FR-8.1).

A report is assembled from stored rows and nothing else. Every figure it
prints came from the warehouse through the semantic layer, and the
sources section lists the documents behind them, so a reader can check
any number without asking anyone.

Charts are inline SVG rather than an image dependency: a brief has to
survive being emailed, archived, and opened years later, and a self
contained file does that where a chart library reference does not.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.common.logging import get_logger

log = get_logger(__name__)


@dataclass
class Brief:
    period: str
    generated_at: str
    headline_rows: list[dict] = field(default_factory=list)
    movers: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    forecasts: list[dict] = field(default_factory=list)
    insights: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)


def _rows(session: Session, sql: str, **params) -> list[dict]:
    return [dict(r) for r in session.execute(text(sql), params).mappings().all()]


def build(session: Session, period: str | None = None) -> Brief:
    """Assemble a brief for one period, defaulting to the latest month."""
    if period is None:
        row = session.execute(text(
            "SELECT period FROM v_cargo_fact WHERE grain='AIRPORT' "
            "AND period_kind='MONTH' ORDER BY sort_key DESC LIMIT 1"
        )).first()
        period = row[0] if row else "unknown"

    brief = Brief(period=period,
                  generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"))

    brief.headline_rows = _rows(session, """
        SELECT airport_iata, airport_name,
               ROUND((SUM(tonnage_kg)/1000)::numeric, 1) AS tonnage_mt,
               CASE WHEN SUM(prior_year_tonnage_kg) > 0
                    THEN ROUND(((SUM(tonnage_kg) - SUM(prior_year_tonnage_kg))
                         / SUM(prior_year_tonnage_kg) * 100)::numeric, 1) END AS yoy_pct
        FROM v_cargo_fact
        WHERE grain='AIRPORT' AND direction='TOTAL' AND period=:p
        GROUP BY 1,2 ORDER BY 3 DESC NULLS LAST LIMIT 10
    """, p=period)

    # Movers are ranked by growth but floored by volume, because a
    # four-tonne airport posting 486% is noise rather than news.
    brief.movers = _rows(session, """
        SELECT airport_iata, airport_name,
               ROUND((SUM(tonnage_kg)/1000)::numeric, 1) AS tonnage_mt,
               ROUND(((SUM(tonnage_kg) - SUM(prior_year_tonnage_kg))
                    / NULLIF(SUM(prior_year_tonnage_kg),0) * 100)::numeric, 1) AS yoy_pct
        FROM v_cargo_fact
        WHERE grain='AIRPORT' AND direction='TOTAL' AND period=:p
        GROUP BY 1,2
        HAVING SUM(tonnage_kg) >= 100000 AND SUM(prior_year_tonnage_kg) > 0
        ORDER BY 4 DESC NULLS LAST LIMIT 8
    """, p=period)

    brief.alerts = _rows(session, """
        SELECT entity_key, COALESCE(airport_name, airline_name) AS entity_name,
               direction, period,
               ROUND((observed_kg/1000)::numeric,1) AS observed_mt,
               ROUND((expected_kg/1000)::numeric,1) AS expected_mt,
               ROUND(deviation_pct::numeric,1) AS deviation_pct, severity
        FROM v_anomaly WHERE period=:p
        ORDER BY abs(deviation_pct) DESC NULLS LAST LIMIT 8
    """, p=period)

    brief.forecasts = _rows(session, """
        SELECT entity_key, COALESCE(airport_name, airline_name) AS entity_name,
               period, ROUND((predicted_kg/1000)::numeric,1) AS predicted_mt,
               ROUND((lower_kg/1000)::numeric,1) AS lower_mt,
               ROUND((upper_kg/1000)::numeric,1) AS upper_mt,
               model, ROUND(backtest_mape::numeric,1) AS mape
        FROM v_forecast WHERE horizon=1 ORDER BY predicted_kg DESC LIMIT 6
    """)

    brief.insights = _rows(session, """
        SELECT headline, narrative FROM v_insight
        ORDER BY insight_id DESC LIMIT 5
    """)

    brief.sources = _rows(session, """
        SELECT publisher, count(*) AS documents, sum(fact_count) AS facts
        FROM v_source GROUP BY publisher ORDER BY 3 DESC NULLS LAST
    """)

    totals = session.execute(text("""
        SELECT ROUND((SUM(tonnage_kg)/1000)::numeric,1),
               count(DISTINCT airport_iata),
               ROUND((SUM(prior_year_tonnage_kg)/1000)::numeric,1)
        FROM v_cargo_fact
        WHERE grain='AIRPORT' AND direction='TOTAL' AND period=:p
    """), {"p": period}).one()
    national, airports, prior = totals
    brief.totals = {
        "national_mt": float(national or 0),
        "airports": int(airports or 0),
        "prior_year_mt": float(prior or 0),
        "yoy_pct": (round((float(national or 0) - float(prior or 0))
                          / float(prior) * 100, 1) if prior else None),
    }
    return brief


# ------------------------------------------------------------- rendering --


def _clip(name: str, width: int = 26) -> str:
    """Truncate with an ellipsis, so a cut name does not read as a typo.

    A hard slice rendered "Indira Gandhi Internationa", which looks like a
    spelling mistake rather than a label that did not fit. The character
    tells the reader something was removed.
    """
    return name if len(name) <= width else name[: width - 1].rstrip() + "\u2026"


def _bar_chart(rows: list[dict], label_key: str, value_key: str,
               width: int = 640, bar_h: int = 22) -> str:
    """A self-contained SVG bar chart.

    Inline rather than a library call, so the brief still renders when
    opened from an archive with no network.
    """
    rows = [r for r in rows if r.get(value_key) is not None][:10]
    if not rows:
        return "<p class='muted'>No data for this period.</p>"
    peak = max(float(r[value_key]) for r in rows) or 1.0
    pad, label_w = 8, 190
    height = len(rows) * (bar_h + 6) + pad * 2
    parts = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="chart" xmlns="http://www.w3.org/2000/svg">'
    ]
    for i, r in enumerate(rows):
        y = pad + i * (bar_h + 6)
        value = float(r[value_key])
        bar_w = max(2, (value / peak) * (width - label_w - 90))
        parts.append(
            f'<text x="0" y="{y + bar_h * 0.7:.0f}" font-size="12" fill="#444">'
            f'{html.escape(_clip(str(r[label_key])))}</text>'
            f'<rect x="{label_w}" y="{y}" width="{bar_w:.0f}" height="{bar_h}" '
            f'rx="3" fill="#1a56db"/>'
            f'<text x="{label_w + bar_w + 6:.0f}" y="{y + bar_h * 0.7:.0f}" '
            f'font-size="12" fill="#444">{value:,.1f}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _pct(v) -> str:
    if v is None:
        return '<span class="muted">n/a</span>'
    cls = "pos" if float(v) >= 0 else "neg"
    return f'<span class="{cls}">{float(v):+,.1f}%</span>'


def render_html(brief: Brief) -> str:
    t = brief.totals
    national = (
        f"{t['national_mt']:,.1f} MT across {t['airports']} airports"
        + (f", {t['yoy_pct']:+,.1f}% year on year" if t.get("yoy_pct") is not None else "")
    )

    def table(rows, cols, headers):
        if not rows:
            return "<p class='muted'>Nothing to report.</p>"
        head = "".join(f"<th>{h}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(
                f"<td>{_pct(r[c]) if c.endswith('_pct') else html.escape(str(r.get(c) if r.get(c) is not None else 'n/a'))}</td>"
                for c in cols) + "</tr>"
            for r in rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"

    insights = "".join(
        f"<div class='insight'><strong>{html.escape(i['headline'])}</strong>"
        f"<p>{html.escape(i['narrative'])}</p></div>"
        for i in brief.insights
    ) or "<p class='muted'>No explanations recorded for this period.</p>"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Air Cargo Brief: {brief.period}</title>
<style>
 body {{ font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        color:#14181f; max-width:860px; margin:32px auto; padding:0 20px; }}
 h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:28px 0 8px;
        border-bottom:1px solid #e2e5ea; padding-bottom:6px; }}
 .meta {{ color:#5c6470; font-size:12px; }}
 .lede {{ background:#f0f4ff; border:1px solid #d7e2ff; border-radius:8px;
        padding:12px 14px; margin:16px 0; }}
 table {{ border-collapse:collapse; width:100%; font-size:13px; }}
 th {{ text-align:left; color:#5c6470; font-weight:500; font-size:11px;
      text-transform:uppercase; letter-spacing:.04em; border-bottom:1px solid #e2e5ea; padding:7px 6px; }}
 td {{ padding:7px 6px; border-bottom:1px solid #eef0f3; font-variant-numeric:tabular-nums; }}
 .pos {{ color:#0f7a3d; }} .neg {{ color:#b4231f; }} .muted {{ color:#5c6470; }}
 .insight {{ background:#fafbfc; border-left:3px solid #1a56db; padding:8px 12px; margin:8px 0; }}
 .insight p {{ margin:4px 0 0; color:#3a424e; }}
 footer {{ margin-top:32px; padding-top:12px; border-top:1px solid #e2e5ea;
          color:#5c6470; font-size:12px; }}
</style></head><body>

<h1>Air Cargo Brief: {brief.period}</h1>
<div class="meta">Generated {brief.generated_at}</div>

<div class="lede"><strong>National total:</strong> {national}</div>

<h2>Largest airports</h2>
{_bar_chart(brief.headline_rows, "airport_name", "tonnage_mt")}
{table(brief.headline_rows, ["airport_iata", "airport_name", "tonnage_mt", "yoy_pct"],
       ["Code", "Airport", "Tonnage (MT)", "YoY"])}

<h2>Fastest growing <span class="meta">at least 100 MT, because percentage growth below that is noise</span></h2>
{table(brief.movers, ["airport_iata", "airport_name", "tonnage_mt", "yoy_pct"],
       ["Code", "Airport", "Tonnage (MT)", "YoY"])}

<h2>Alerts</h2>
{table(brief.alerts, ["entity_name", "direction", "observed_mt", "expected_mt", "deviation_pct", "severity"],
       ["Entity", "Direction", "Observed", "Expected", "Deviation", "Severity"])}

<h2>Explanations</h2>
{insights}

<h2>Forecasts <span class="meta">80% interval, with backtest error</span></h2>
{table(brief.forecasts, ["entity_name", "period", "predicted_mt", "lower_mt", "upper_mt", "model", "mape"],
       ["Entity", "Period", "Predicted", "Lower", "Upper", "Model", "MAPE %"])}

<h2>Sources</h2>
{table(brief.sources, ["publisher", "documents", "facts"], ["Publisher", "Documents", "Facts"])}

<footer>Every figure in this brief was computed in SQL over the warehouse and
traces to one of the source documents listed above. No number was generated
by a language model.</footer>
</body></html>"""


def render_markdown(brief: Brief) -> str:
    """A plain-text form, for pasting into an email or a ticket."""
    t = brief.totals
    lines = [
        f"# Air Cargo Brief: {brief.period}", "",
        f"_Generated {brief.generated_at}_", "",
        f"**National total:** {t['national_mt']:,.1f} MT across {t['airports']} airports"
        + (f", {t['yoy_pct']:+,.1f}% year on year" if t.get("yoy_pct") is not None else ""),
        "", "## Largest airports", "",
        "| Code | Airport | Tonnage (MT) | YoY |", "|---|---|---:|---:|",
    ]
    for r in brief.headline_rows:
        yoy = f"{float(r['yoy_pct']):+,.1f}%" if r.get("yoy_pct") is not None else "n/a"
        lines.append(f"| {r['airport_iata']} | {r['airport_name']} | {r['tonnage_mt']:,} | {yoy} |")

    lines += ["", "## Alerts", ""]
    if brief.alerts:
        lines += ["| Entity | Period | Observed | Expected | Deviation | Severity |",
                  "|---|---|---:|---:|---:|---|"]
        for a in brief.alerts:
            lines.append(
                f"| {a['entity_name'] or a['entity_key']} | {a['period']} | "
                f"{a['observed_mt']} | {a['expected_mt']} | {a['deviation_pct']}% | {a['severity']} |"
            )
    else:
        lines.append("_Nothing flagged._")

    if brief.insights:
        lines += ["", "## Explanations", ""]
        for i in brief.insights:
            lines += [f"**{i['headline']}**", "", i["narrative"], ""]

    lines += ["", "## Sources", ""]
    for s in brief.sources:
        lines.append(f"- {s['publisher']}: {s['documents']} documents, {s['facts']} facts")
    lines += ["", "_Every figure traces to one of the documents above._"]
    return "\n".join(lines)
