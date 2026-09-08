/* Dashboard client.
 *
 * Every figure shown here is fetched from the API, which computes it in
 * SQL over the semantic layer. Nothing is derived in the browser: a
 * number recomputed client-side is a second definition of a metric, and
 * two definitions eventually disagree.
 */

const API = "/api/v1";
const $ = (id) => document.getElementById(id);

const fmt = {
  mt: (v) => v == null ? "—" : Number(v).toLocaleString(undefined,
        { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
  int: (v) => v == null ? "—" : Number(v).toLocaleString(),
  pct: (v) => v == null ? "—" : `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(1)}%`,
};

async function api(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

/* A percentage pill, coloured by direction. Null growth is shown as a
   dash rather than as zero: "unknown" and "no change" are different
   facts and conflating them misleads. */
function pill(v) {
  if (v == null) return `<span class="pill flat">—</span>`;
  const cls = v > 0 ? "pos" : v < 0 ? "neg" : "flat";
  return `<span class="pill ${cls}">${fmt.pct(v)}</span>`;
}

/* ------------------------------------------------------------- theme -- */
(function theme() {
  const saved = (() => { try { return localStorage.getItem("aci-theme"); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $("theme-toggle").onclick = () => {
    const now = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = now;
    try { localStorage.setItem("aci-theme", now); } catch { /* private mode */ }
    if (airlineChart) renderAirlines(lastAirlineRows);
  };
})();

/* --------------------------------------------------------------- kpis -- */
async function loadKpis() {
  const [health, sources] = await Promise.all([api("/health"), api("/sources")]);
  const facts = sources.publishers.reduce((a, p) => a + Number(p.facts || 0), 0);
  $("kpis").innerHTML = [
    ["Facts", fmt.int(health.facts), "each traced to a source document"],
    ["Airports", fmt.int(health.airports), "resolved to IATA/ICAO"],
    ["Airlines", fmt.int(health.airlines), "carriers and industry totals"],
    ["Periods", fmt.int(health.periods), "months, fiscal and annual"],
    ["Publishers", fmt.int(sources.publishers.length), `${fmt.int(facts)} facts attributed`],
  ].map(([label, value, note]) => `
    <div class="kpi"><div class="label">${label}</div>
      <div class="value">${value}</div><div class="note">${note}</div></div>`).join("");
  $("freshness").textContent = `${fmt.int(health.facts)} facts · ${health.database}`;
}

/* --------------------------------------------------------- rankings -- */
async function loadPeriods() {
  const d = await api("/airports/trend?iata=DEL");
  const periods = [...new Set(d.rows.map((r) => r.period))].sort().reverse();
  $("rank-period").innerHTML =
    `<option value="">Latest period</option>` +
    periods.map((p) => `<option value="${p}">${p}</option>`).join("");
}

async function loadRankings() {
  const order = $("rank-order").value;
  const direction = $("rank-direction").value;
  const period = $("rank-period").value;
  const q = new URLSearchParams({ order_by: order, direction, limit: "12" });
  if (period) q.set("period", period);

  $("rankings").innerHTML = `<div class="empty">loading…</div>`;
  const d = await api(`/airports/rankings?${q}`);

  $("ranking-hint").textContent = `${d.row_count} shown`;
  $("rank-note").textContent = order === "growth_yoy_pct"
    ? "≥100 MT — percentage growth is noise below that"
    : "";

  if (!d.rows.length) {
    $("rankings").innerHTML = `<div class="empty">No data for this selection.</div>`;
    return;
  }
  $("rankings").innerHTML = `
    <table><thead><tr>
      <th>Airport</th><th>Code</th><th class="num">Tonnage</th>
      <th class="num">YoY</th><th class="num">Sources</th>
    </tr></thead><tbody>
    ${d.rows.map((r) => `
      <tr>
        <td>${r.airport_name ?? "—"}</td>
        <td><span class="code">${r.airport_iata ?? "—"}</span></td>
        <td class="num">${fmt.mt(r.tonnage_mt)} <span class="dim">MT</span></td>
        <td class="num">${pill(r.growth_yoy_pct)}</td>
        <td class="num dim">${fmt.int(r.source_count)}</td>
      </tr>`).join("")}
    </tbody></table>`;
}

/* ---------------------------------------------------------- airlines -- */
let airlineChart = null;
let lastAirlineRows = [];

function renderAirlines(rows) {
  lastAirlineRows = rows;
  const css = getComputedStyle(document.documentElement);
  const text = css.getPropertyValue("--text-dim").trim();
  const grid = css.getPropertyValue("--border").trim();
  const accent = css.getPropertyValue("--accent").trim();

  if (airlineChart) airlineChart.destroy();
  airlineChart = new Chart($("airline-chart"), {
    type: "bar",
    data: {
      labels: rows.map((r) => r.airline_name),
      datasets: [{ label: "MT", data: rows.map((r) => r.tonnage_mt),
                   backgroundColor: accent, borderRadius: 4 }],
    },
    options: {
      indexAxis: "y", maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (c) => `${fmt.mt(c.raw)} MT` } } },
      scales: {
        x: { ticks: { color: text, callback: (v) => fmt.int(v) }, grid: { color: grid } },
        y: { ticks: { color: text }, grid: { display: false } },
      },
    },
  });
}

async function loadAirlines() {
  const d = await api("/airlines/share?limit=8");
  renderAirlines(d.rows);
}

/* ------------------------------------------------------------ alerts -- */
async function loadAlerts() {
  const severity = $("alert-severity").value;
  const grain = $("alert-grain").value;
  const q = new URLSearchParams({ limit: "25" });
  if (severity === "HIGH") q.set("severity", "HIGH");
  if (grain) q.set("grain", grain);

  $("alerts").innerHTML = `<div class="empty">loading…</div>`;
  let d = await api(`/anomalies?${q}`);
  let rows = d.rows;
  // "Medium and above" is two severities, and the API filters on one, so
  // the union is assembled here rather than adding a second parameter.
  if (severity === "MEDIUM") rows = rows.filter((r) => r.severity !== "LOW");

  $("alert-count").textContent = `${rows.length} flagged`;
  if (!rows.length) {
    $("alerts").innerHTML = `<div class="empty">Nothing flagged for this filter.</div>`;
    return;
  }
  $("alerts").innerHTML = rows.map((r) => `
    <div class="alert ${r.severity}">
      <div class="bar"></div>
      <div class="content">
        <div class="top">
          <span class="who">${r.entity_name ?? r.entity_key}</span>
          <span class="code">${r.entity_key}</span>
          <span class="meta">${r.period} · ${r.direction.toLowerCase()}</span>
          <span class="spacer"></span>
          ${pill(r.deviation_pct)}
        </div>
        <div class="detail">
          observed ${fmt.mt(r.observed_mt)} MT against an expected
          ${fmt.mt(r.expected_mt)} MT · ${r.method} · z=${r.z_score ?? "—"}
        </div>
      </div>
    </div>`).join("");
}

/* --------------------------------------------------------- forecasts -- */
async function loadForecasts() {
  const d = await api("/forecasts?horizon=1&limit=8");
  if (!d.rows.length) {
    $("forecasts").innerHTML = `<div class="empty">No forecasts yet.</div>`;
    return;
  }
  $("forecasts").innerHTML = `
    <table><thead><tr>
      <th>Entity</th><th>Period</th><th class="num">Predicted</th>
      <th class="num">80% interval</th><th class="num">MAPE</th>
    </tr></thead><tbody>
    ${d.rows.map((r) => `
      <tr>
        <td>${r.entity_name ?? r.entity_key}<div class="dim" style="font-size:11px">${r.model}</div></td>
        <td class="dim">${r.period}</td>
        <td class="num">${fmt.mt(r.predicted_mt)}</td>
        <td class="num dim">${fmt.mt(r.lower_mt)} – ${fmt.mt(r.upper_mt)}</td>
        <td class="num">${r.backtest_mape_pct == null ? "—" : r.backtest_mape_pct + "%"}</td>
      </tr>`).join("")}
    </tbody></table>`;
}

/* ----------------------------------------------------------- sources -- */
async function loadSources() {
  const d = await api("/sources");
  $("sources").innerHTML = `
    <table><thead><tr><th>Publisher</th><th class="num">Documents</th><th class="num">Facts</th></tr></thead>
    <tbody>${d.publishers.map((p) => `
      <tr><td>${p.publisher}</td><td class="num">${fmt.int(p.documents)}</td>
      <td class="num">${fmt.int(p.facts)}</td></tr>`).join("")}
    </tbody></table>`;
  $("footer").textContent =
    "Every figure is computed in SQL over a governed semantic layer and carries "
    + "the source document behind it. No number on this page was generated by a model.";
}

/* -------------------------------------------------------------- chat -- */
const SUGGESTIONS = [
  "Show the top 5 cargo airports by growth",
  "Which airlines carry the most cargo?",
  "What anomalies have been detected?",
  "Predict cargo demand next quarter",
  "Where does this data come from?",
];

function renderSuggestions() {
  $("suggestions").innerHTML = SUGGESTIONS
    .map((s) => `<button class="ghost" data-q="${s}">${s}</button>`).join("");
  $("suggestions").onclick = (e) => {
    const q = e.target.dataset.q;
    if (q) { $("chat-input").value = q; $("chat-form").requestSubmit(); }
  };
}

async function ask(question) {
  const log = $("chat-log");
  if (log.querySelector(".empty")) log.innerHTML = "";
  log.insertAdjacentHTML("beforeend", `<div class="msg q">${question}</div>`);
  log.scrollTop = log.scrollHeight;

  let d;
  try {
    const r = await fetch(`${API}/chat/query`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    d = await r.json();
    if (!r.ok) throw new Error(d.detail ?? r.statusText);
  } catch (err) {
    log.insertAdjacentHTML("beforeend", `<div class="msg">Request failed: ${err.message}</div>`);
    return;
  }

  const badge = d.grounded
    ? `<span class="badge-grounded">grounded</span>`
    : `<span class="badge-grounded badge-ungrounded">unverified</span>`;
  const cites = (d.citations ?? []).length
    ? `<div class="cites">${d.citations.length} source document(s) · `
      + `<a href="${d.citations[0].source_url}" target="_blank" rel="noopener">`
      + `${d.citations[0].publisher}</a></div>`
    : "";
  log.insertAdjacentHTML("beforeend", `
    <div class="msg">
      <div>${badge} ${d.answer}</div>
      <div class="read-as">read as: ${d.understood_as}</div>
      ${cites}
    </div>`);
  log.scrollTop = log.scrollHeight;
}

/* -------------------------------------------------------------- boot -- */
function wire() {
  $("rank-order").onchange = loadRankings;
  $("rank-direction").onchange = loadRankings;
  $("rank-period").onchange = loadRankings;
  $("alert-severity").onchange = loadAlerts;
  $("alert-grain").onchange = loadAlerts;
  $("chat-form").onsubmit = (e) => {
    e.preventDefault();
    const v = $("chat-input").value.trim();
    if (v.length >= 3) { ask(v); $("chat-input").value = ""; }
  };
}

(async function boot() {
  wire();
  renderSuggestions();
  // Each panel fails independently: one endpoint being down should not
  // leave the whole dashboard blank.
  const panels = [
    ["kpis", loadKpis], ["rankings", loadRankings], ["airline-chart", loadAirlines],
    ["alerts", loadAlerts], ["forecasts", loadForecasts], ["sources", loadSources],
    ["rank-period", loadPeriods],
  ];
  await Promise.all(panels.map(async ([id, fn]) => {
    try { await fn(); }
    catch (err) {
      console.error(id, err);
      const el = $(id);
      if (el && el.tagName !== "CANVAS" && el.tagName !== "SELECT") {
        el.innerHTML = `<div class="empty">Could not load: ${err.message}</div>`;
      }
    }
  }));
})();
