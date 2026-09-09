/* Air Cargo Intelligence dashboard.

   No framework and no build step: the API serves this directory, so there
   is nothing to compile before the project runs. Charts are hand-drawn SVG
   rather than a charting dependency, which keeps the page loading no third
   party code and keeps the forecast band drawn exactly as intended.

   This file formats and lays out. It never computes a cargo figure: every
   number arrives from the API already calculated, and the one place that
   could be tempted to derive one - the plain-language summaries - reads
   fields the API returned rather than recomputing them.                   */

'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const on = (sel, ev, fn) => {
  const el = typeof sel === 'string' ? $(sel) : sel;
  if (el) el.addEventListener(ev, fn);
  return el;
};

const api = async (path, opts) => {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
};

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const n1 = (v) => (v == null || v === '' || Number.isNaN(Number(v)))
  ? 'n/a' : Number(v).toLocaleString('en-IN', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const n0 = (v) => (v == null || Number.isNaN(Number(v)))
  ? 'n/a' : Math.round(Number(v)).toLocaleString('en-IN');
const int = n0;

/* One scale for a whole axis, chosen from its largest value.

   Formatting each tick independently produced an axis reading 1.1L, 1.0L,
   88.8k, 76.5k - two different units stacked on one scale, which forces the
   reader to convert in their head to see that the gaps are even. */
const axisScale = (max) => {
  const m = Math.abs(max);
  if (m >= 1e5) return { div: 1e5, suffix: 'L' };   // lakh
  if (m >= 1e3) return { div: 1e3, suffix: 'k' };
  return { div: 1, suffix: '' };
};
const onScale = (v, sc) => {
  const x = Number(v) / sc.div;
  if (!Number.isFinite(x)) return 'n/a';
  return (Math.abs(x) >= 100 ? x.toFixed(0) : x.toFixed(1)) + sc.suffix;
};

const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const prettyPeriod = (p) => {
  const m = /^(\d{4})-(\d{2})$/.exec(p || '');
  if (m) return `${MONTHS[+m[2] - 1]} ${m[1]}`;
  const fy = /^(\d{4})-FY$/.exec(p || '');
  if (fy) return `FY ${fy[1]}`;
  return p || 'n/a';
};

const ago = (iso) => {
  if (!iso) return 'unknown';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

/* Plain language. A percentage on its own does not tell a reader what
   changed relative to what, which was most of why the figures read as
   opaque. */
const plainDelta = (pct) => {
  const p = Number(pct);
  if (!Number.isFinite(p)) return 'no comparable month a year earlier';
  const dir = p >= 0 ? 'up' : 'down';
  return `${dir} ${Math.abs(p).toFixed(1)}% on the same month last year`;
};

const METHOD_LABEL = {
  stl_residual: 'seasonal residual',
  robust_z: 'robust z-score',
  consensus: 'both methods agree',
  service_started: 'service started',
  service_stopped: 'service stopped',
};
const methodLabel = (m) => METHOD_LABEL[m] || String(m || '').replace(/_/g, ' ');

// Series are monthly or fiscal-annual, and the window is three periods
// either way. Saying "three months at zero" of a fiscal year understates a
// three-year absence by a factor of twelve.
const periodUnit = (p) => (/FY/i.test(String(p)) ? 'years' : 'months');
const perPeriod  = (p) => (/FY/i.test(String(p)) ? 'a year' : 'a month');

const plainAnomaly = (r) => {
  const obs = Number(r.observed_mt), exp = Number(r.expected_mt);
  const when = prettyPeriod(r.period);

  // A service starting or stopping carries no deviation percentage, because
  // there is no ratio to a baseline of zero. Number(null) is 0, which read
  // as "far more than the 0.0 MT its seasonal pattern implied" - and for a
  // service stopping, as more than nothing when it handled nothing.
  if (r.method === 'service_started') {
    return `Began handling cargo in ${when}, reaching ${n1(obs)} MT after `
         + `three ${periodUnit(r.period)} at zero.`;
  }
  if (r.method === 'service_stopped') {
    return `Stopped handling cargo after ${when}, having been running at `
         + `about ${n1(exp)} MT ${perPeriod(r.period)}.`;
  }
  if (r.deviation_pct === null || r.deviation_pct === undefined) {
    return `Departed from its usual pattern in ${when}, handling ${n1(obs)} MT.`;
  }

  const dir = Number(r.deviation_pct) >= 0 ? 'far more' : 'far less';
  return `Handled ${n1(obs)} MT in ${when}, ${dir} than the `
       + `${n1(exp)} MT its seasonal pattern implied.`;
};

/* ------------------------------------------------------------- routing */

const VIEWS = {
  overview: ['Overview', 'Cargo throughput across Indian and international airports.'],
  airport:  ['Airport detail', 'History, projection and flagged months for one airport.'],
  operations: ['Operations', 'How efficiently freight moved, and which airports drove the national change.'],
  forecast: ['Forecasts', 'Where the statistical models expect traffic to go, and how wrong they have been before.'],
  alerts:   ['Alerts', 'Months that departed from the seasonal pattern, and why that matters.'],
  ask:      ['Ask the data', 'Questions answered by SQL over a governed semantic layer.'],
  brief:    ['Monthly brief', 'The standing report, generated from the same figures.'],
  agents:   ['Agent console', 'What each agent decided, why, and which policy decided it.'],
  sources:  ['Sources & health', 'Every figure traces to a published document.'],
};

const loaded = new Set();
const loadedTabs = new Set();

/* Which loader belongs to which tab.

   Splitting these out is not only layout: opening Operations used to fire
   five requests at once for panels the reader had not asked for. Each tab
   now fetches its own data the first time it is opened, and never again. */
const TAB_LOADERS = {
  operations: {
    growth: () => drawAttribution(),
    airports: () => drawAirportEfficiency(),
    carriers: () => { drawBelly(); drawEfficiency(); },
    market: () => drawConcentration(),
  },
  // The airport view's panels are all drawn by drawAirport for the selected
  // airport, so its tabs share one loader rather than three.
  
  alerts: {
    flagged: () => loadAlerts(),
    written: () => loadInsights(),
  },
  agents: {
    replay: () => {},          // populated by loadRuns as part of the view
    runs: () => {},
    tools: () => {},
  },
  sources: {
    provenance: () => loadPublishers(),
    search: () => {},
    health: () => loadHealth(),
  },
  airport: {
    trend: () => {},
    drivers: () => {},
    flags: () => {},
  },
};

/* Forget what a view's tabs have loaded.

   A control that applies to the whole view changes what every tab should
   show. Without this, choosing a different airport on one tab and moving to
   another still displayed the previous airport's figures - the tab had
   already loaded once and never reconsidered. */
function invalidateTabs(view, { except = null } = {}) {
  for (const key of [...loadedTabs]) {
    if (key.startsWith(`${view}/`) && key !== `${view}/${except}`) loadedTabs.delete(key);
  }
}

function refreshView(view) {
  const active = currentTab(view);
  invalidateTabs(view, { except: null });
  loadedTabs.add(`${view}/${active}`);
  try { (TAB_LOADERS[view]?.[active] || (() => {}))(); }
  catch (e) { console.error('refresh failed', view, active, e); }
}

function currentTab(view) {
  const nav = $(`.subtabs[data-for="${view}"]`);
  return nav ? $('.subtab.is-active', nav)?.dataset.tab : null;
}

function selectTab(view, tab, { pushHash = true } = {}) {
  const nav = $(`.subtabs[data-for="${view}"]`);
  if (!nav) return;
  const section = $(`.view[data-view="${view}"]`);
  const buttons = $$('.subtab', nav);
  const wanted = buttons.some((b) => b.dataset.tab === tab) ? tab : buttons[0]?.dataset.tab;
  if (!wanted) return;

  buttons.forEach((b) => b.classList.toggle('is-active', b.dataset.tab === wanted));
  $$('.tabpanel', section).forEach((pane) =>
    pane.classList.toggle('is-active', pane.dataset.tab === wanted));

  const key = `${view}/${wanted}`;
  if (!loadedTabs.has(key)) {
    loadedTabs.add(key);
    try { (TAB_LOADERS[view]?.[wanted] || (() => {}))(); }
    catch (e) { console.error('tab loader failed', key, e); }
  }
  if (pushHash) {
    const target = `${view}/${wanted}`;
    if (location.hash.slice(1) !== target) {
      history.replaceState(null, '', `#${target}`);
    }
  }
}

function show(route) {
  const [view0, tab] = String(route || '').split('/');
  const view = VIEWS[view0] ? view0 : 'overview';
  $$('.nav-item').forEach((b) => b.classList.toggle('is-active', b.dataset.view === view));
  $$('.view').forEach((s) => s.classList.toggle('is-active', s.dataset.view === view));
  const [title, sub] = VIEWS[view];
  $('#view-title').textContent = title;
  $('#view-sub').textContent = sub;
  if (!loaded.has(view)) { loaded.add(view); (LOADERS[view] || (() => {}))(); }
  // After the view loader, so a tab loader can rely on view-level state.
  selectTab(view, tab || currentTab(view), { pushHash: false });
  const target = $(`.subtabs[data-for="${view}"]`) ? `${view}/${currentTab(view)}` : view;
  if (location.hash.slice(1) !== target) history.replaceState(null, '', `#${target}`);
  window.scrollTo({ top: 0 });
}

/* --------------------------------------------------------------- chart */

/* History, projection and interval in one SVG.

   The band is drawn first so the lines sit on top of it, and the forecast
   line starts at the last observed point rather than floating detached -
   a projection that does not visibly continue the series reads as an
   unrelated second chart. */
function seriesChart(history, forecast, anomalies = []) {
  const W = 860, H = 320;
  const pad = { l: 58, r: 18, t: 18, b: 42 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;

  const hist = history.map((r) => ({ period: r.period, v: Number(r.tonnage_mt) }))
                      .filter((p) => Number.isFinite(p.v));
  const fc = forecast.map((r) => ({
    period: r.period, v: Number(r.predicted_mt),
    lo: Number(r.lower_mt), hi: Number(r.upper_mt),
  })).filter((p) => Number.isFinite(p.v));

  if (!hist.length) return '<div class="empty">no history for this selection</div>';

  const all = [...hist.map((p) => p.period), ...fc.map((p) => p.period)];
  const vals = [...hist.map((p) => p.v), ...fc.flatMap((p) => [p.lo, p.hi, p.v])]
                 .filter(Number.isFinite);
  const min = Math.min(...vals), max = Math.max(...vals);
  const lo = Math.max(0, min - (max - min) * 0.12);
  const hi = max + (max - min) * 0.12;

  const x = (i) => pad.l + (all.length < 2 ? iw / 2 : (i / (all.length - 1)) * iw);
  const y = (v) => pad.t + ih - ((v - lo) / (hi - lo || 1)) * ih;

  const anomAt = new Map(anomalies.map((a) => [a.period, a]));

  // y gridlines, all ticks on one scale
  const sc = axisScale(hi);
  let gridSvg = '';
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    const yy = y(v);
    gridSvg += `<line class="grid-line" x1="${pad.l}" y1="${yy}" x2="${W - pad.r}" y2="${yy}"/>`
             + `<text class="ax-text" x="${pad.l - 8}" y="${yy + 3.5}" text-anchor="end">${onScale(v, sc)}</text>`;
  }

  // x labels. Thinning by modulo alone still collided, because the final
  // label is always drawn and can land a few pixels from the previous one.
  // Track the last x actually used and require real separation.
  const every = Math.max(1, Math.ceil(all.length / 8));
  const MIN_GAP = 62;
  let xSvg = '';
  let lastX = -Infinity;
  all.forEach((p, i) => {
    const isLast = i === all.length - 1;
    if (i % every && !isLast) return;
    const px = x(i);
    if (px - lastX < MIN_GAP) {
      // Keep the endpoint in preference to the tick before it: the end of
      // the projection is the label a reader most wants anchored.
      if (!isLast) return;
      xSvg = xSvg.replace(/<text class="ax-text"[^>]*>[^<]*<\/text>$/, '');
    }
    lastX = px;
    xSvg += `<text class="ax-text" x="${px}" y="${H - pad.b + 17}" text-anchor="middle">${esc(prettyPeriod(p))}</text>`;
  });

  const histPath = hist.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join('');

  let bandSvg = '', fcPath = '', fcPts = '';
  if (fc.length) {
    const base = hist.length - 1;
    const up = fc.map((p, i) => `${x(base + 1 + i).toFixed(1)},${y(p.hi).toFixed(1)}`);
    const dn = fc.map((p, i) => `${x(base + 1 + i).toFixed(1)},${y(p.lo).toFixed(1)}`).reverse();
    const anchor = `${x(base).toFixed(1)},${y(hist[base].v).toFixed(1)}`;
    bandSvg = `<polygon class="band" points="${anchor} ${up.join(' ')} ${dn.join(' ')}"/>`;
    fcPath = `M${anchor}` + fc.map((p, i) => `L${x(base + 1 + i).toFixed(1)},${y(p.v).toFixed(1)}`).join('');
    fcPts = fc.map((p, i) => `<circle class="pt-fc" cx="${x(base + 1 + i)}" cy="${y(p.v)}" r="3.5"/>`).join('');
  }

  const histPts = hist.map((p, i) => {
    const a = anomAt.get(p.period);
    return a
      ? `<circle class="pt-anom" cx="${x(i)}" cy="${y(p.v)}" r="5"><title>${esc(plainAnomaly(a))}</title></circle>`
      : `<circle class="pt-hist" cx="${x(i)}" cy="${y(p.v)}" r="2.4" opacity=".75"/>`;
  }).join('');

  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Freight tonnage history and projection">
    ${gridSvg}
    ${bandSvg}
    <path class="line-hist" d="${histPath}"/>
    ${fcPath ? `<path class="line-fc" d="${fcPath}"/>` : ''}
    ${histPts}${fcPts}
    <line class="ax-line" x1="${pad.l}" y1="${pad.t + ih}" x2="${W - pad.r}" y2="${pad.t + ih}"/>
    ${xSvg}
  </svg>`;
}

const chartLegend = (hasFc, hasAnom) => `
  <div class="key"><span class="swatch" style="background:var(--indigo)"></span> Observed</div>
  ${hasFc ? '<div class="key"><span class="swatch dash"></span> Projected (SARIMA)</div>' : ''}
  ${hasFc ? '<div class="key"><span class="swatch band" style="background:var(--saffron)"></span> 80% interval</div>' : ''}
  ${hasAnom ? '<div class="key"><span class="swatch dot" style="background:var(--rose)"></span> Flagged month</div>' : ''}`;

/* ------------------------------------------------------------ overview */

async function loadOverview() {
  try {
    const h = await api('/api/v1/health');
    $('#kpis').innerHTML = [
      ['Facts held', int(h.facts), 'each traced to a document', true],
      ['Airports', int(h.airports), 'resolved to IATA codes', false],
      ['Airlines', int(h.airlines), 'carriers tracked', false],
      ['Periods', int(h.periods), 'months and fiscal years', false],
    ].map(([l, v, note, accent]) => `
      <div class="kpi${accent ? ' accent' : ''}">
        <div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-note">${note}</div>
      </div>`).join('');
  } catch { $('#kpis').innerHTML = '<div class="empty">could not reach the API</div>'; }
  loadRankings(); loadAirlines();
}

async function loadRankings() {
  const el = $('#rankings');
  el.innerHTML = '<div class="loading">loading…</div>';
  try {
    const d = await api(`/api/v1/airports/rankings?limit=10&direction=${$('#rank-direction').value}&order=${$('#rank-order').value}`);
    if (!d.rows.length) return void (el.innerHTML = '<div class="empty">no rows for this selection</div>');
    const max = Math.max(...d.rows.map((r) => Number(r.tonnage_mt) || 0));
    el.innerHTML = `<div class="rows">${d.rows.map((r, i) => {
      const g = Number(r.growth_yoy_pct);
      const cls = Number.isFinite(g) ? (g >= 0 ? 'up' : 'down') : '';
      const sign = Number.isFinite(g) && g >= 0 ? '+' : '';
      return `<div class="row rank-row clickable" data-iata="${esc(r.airport_iata || '')}"
                   title="${esc(r.airport_name)}: ${esc(plainDelta(g))}">
        <span class="rank">${i + 1}</span>
        <span class="code">${esc(r.airport_iata || 'n/a')}</span>
        <span class="name"><span class="name-main">${esc(r.airport_name)}</span>
          <div class="bar-wrap"><div class="bar" style="width:${max ? (Number(r.tonnage_mt) / max) * 100 : 0}%"></div></div>
        </span>
        <span class="num">${n1(r.tonnage_mt)}</span>
        <span class="delta ${cls}">${Number.isFinite(g) ? sign + g.toFixed(1) + '%' : 'n/a'}</span>
      </div>`;
    }).join('')}</div>`;
    $$('.rank-row', el).forEach((row) => on(row, 'click', () => {
      if (row.dataset.iata) openAirport(row.dataset.iata);
    }));
  } catch { el.innerHTML = '<div class="empty">could not load rankings</div>'; }
}

async function loadAirlines() {
  const el = $('#airline-chart');
  try {
    const d = await api('/api/v1/airlines/share?limit=7');
    if (!d.rows.length) return void (el.innerHTML = '<div class="empty">no rows</div>');
    const max = Math.max(...d.rows.map((r) => Number(r.tonnage_mt)));
    const rowH = 34, h = d.rows.length * rowH + 10;
    el.innerHTML = `<div class="chart"><svg viewBox="0 0 500 ${h}" role="img" aria-label="Airline cargo share">
      ${d.rows.map((r, i) => {
        const y = 6 + i * rowH, w = max ? (Number(r.tonnage_mt) / max) * 230 : 0;
        return `<text class="b-label" x="0" y="${y + 15}">${esc(String(r.airline_name).slice(0, 17))}</text>
          <rect class="b-rect" x="150" y="${y + 4}" width="${w}" height="15" rx="3" opacity="${1 - i * 0.085}"/>
          <text class="b-value" x="${150 + w + 8}" y="${y + 16}">${n0(r.tonnage_mt)}</text>`;
      }).join('')}
    </svg></div>`;
  } catch { el.innerHTML = '<div class="empty">could not load chart</div>'; }
}

/* ------------------------------------------------------ airport detail */

let airportList = [];

async function loadAirportView() {
  try {
    const d = await api('/api/v1/airports/rankings?limit=40');
    airportList = d.rows.filter((r) => r.airport_iata);
    $('#airport-pick').innerHTML = airportList
      .map((r) => `<option value="${esc(r.airport_iata)}">${esc(r.airport_iata)} · ${esc(r.airport_name)}</option>`).join('');
    if (airportList.length) drawAirport(airportList[0].airport_iata);
  } catch { $('#airport-chart').innerHTML = '<div class="empty">could not load airports</div>'; }
}

function openAirport(iata) {
  show('airport');
  const apply = () => { $('#airport-pick').value = iata; drawAirport(iata); };
  airportList.length ? apply() : loadAirportView().then(apply);
}

async function drawAirport(iata) {
  const dir = $('#airport-direction').value;
  $('#airport-chart').innerHTML = '<div class="loading">loading…</div>';
  try {
    const [trend, fc, anom] = await Promise.all([
      api(`/api/v1/airports/trend?iata=${encodeURIComponent(iata)}&direction=${dir}`),
      api('/api/v1/forecasts?grain=AIRPORT&limit=200').catch(() => ({ rows: [] })),
      api('/api/v1/anomalies?grain=AIRPORT&limit=200').catch(() => ({ rows: [] })),
    ]);

    const mine = fc.rows.filter((r) => r.entity_key === iata && r.direction === dir)
                       .sort((a, b) => a.horizon - b.horizon);
    const myAnom = anom.rows.filter((r) => r.entity_key === iata && r.direction === dir);
    const meta = airportList.find((r) => r.airport_iata === iata);

    $('#airport-title').textContent = meta ? `${meta.airport_name} (${iata})` : iata;
    $('#airport-chart').innerHTML = seriesChart(trend.rows, mine, myAnom);
    $('#airport-legend').innerHTML = chartLegend(mine.length > 0, myAnom.length > 0);

    const model = mine[0]?.model, mape = Number(mine[0]?.backtest_mape_pct);
    $('#airport-foot').innerHTML = mine.length
      ? `Projected with <strong>${esc(model)}</strong>. In backtesting its predictions were typically within
         <strong>${mape.toFixed(1)}%</strong> of what actually happened.`
      : 'No projection for this series: too few periods to fit and validate a model.';

    const last = trend.rows[trend.rows.length - 1];
    const first = trend.rows[0];
    $('#airport-summary').innerHTML = last ? `
      <p>In <strong>${esc(prettyPeriod(last.period))}</strong> this airport handled
         <strong>${n1(last.tonnage_mt)} MT</strong> of freight, ${esc(plainDelta(last.growth_yoy_pct))}.</p>
      <p>The series held here runs from ${esc(prettyPeriod(first.period))} to
         ${esc(prettyPeriod(last.period))}, ${trend.rows.length} months in total.</p>
      ${mine.length ? `<p>The model projects <strong>${n1(mine[0].predicted_mt)} MT</strong> for
         ${esc(prettyPeriod(mine[0].period))}, and would not be surprised by anything between
         ${n1(mine[0].lower_mt)} and ${n1(mine[0].upper_mt)} MT.</p>` : ''}
      ${myAnom.length ? `<p><strong>${myAnom.length}</strong> month${myAnom.length > 1 ? 's have' : ' has'}
         been flagged as departing from the seasonal pattern.</p>` : '<p>No month here has been flagged as unusual.</p>'}` : '<p>No history held.</p>';

    drawDecomposition(iata, dir);

    $('#airport-alerts').innerHTML = myAnom.length
      ? myAnom.slice(0, 6).map((r) => `<div class="alert">
          <span class="alert-sev ${String(r.severity).toLowerCase() === 'high' ? 'high' : 'med'}"></span>
          <div class="alert-body">
            <div class="alert-top"><span class="alert-name">${esc(prettyPeriod(r.period))}</span>
              <span class="pill ${String(r.severity).toLowerCase() === 'high' ? 'bad' : 'warn'}">${esc(r.severity)}</span>
              <span class="pill">${esc(methodLabel(r.method))}</span></div>
            <div class="alert-plain">${esc(plainAnomaly(r))}</div>
          </div></div>`).join('')
      : '<div class="empty">Nothing flagged for this airport.</div>';
  } catch { $('#airport-chart').innerHTML = '<div class="empty">could not load this airport</div>'; }
}

/* ---------------------------------------------------------- operations */

/* Each panel is fetched by its own tab loader, so nothing is requested for
   a panel the reader has not opened. */
const opsDirection = () => $('#ops-direction')?.value || 'TOTAL';

async function loadOperations() {}

async function drawAirportEfficiency() {
  const el = $('#airport-efficiency');
  el.innerHTML = '<div class="loading">loading…</div>';
  try {
    const d = await api(`/api/v1/operations/airport-efficiency?limit=18&direction=${opsDirection()}`);
    if (!d.rows.length) { el.innerHTML = '<div class="empty">no paired flight data</div>'; return; }
    const maxT = Math.max(...d.rows.map((r) => Number(r.tonnes_per_flight) || 0), 0.01);
    el.innerHTML = `<div class="rows">${d.rows.map((r) => `
      <div class="row ae-row clickable" data-iata="${esc(r.airport_iata)}">
        <span class="code">${esc(r.airport_iata)}</span>
        <span class="name"><span class="name-main">${esc(r.airport_name || r.airport_iata)}</span>
          <div class="alert-detail">${n0(r.freight_mt)} MT · ${n0(r.movements)} flights · ${n0(r.pax)} pax</div>
          <div class="lf-bar"><span style="width:${(Number(r.tonnes_per_flight) / maxT) * 100}%"></span></div></span>
        <span class="num">${Number(r.tonnes_per_flight).toFixed(2)} t</span>
        <span class="num">${Number(r.kg_per_pax).toFixed(1)} kg</span>
        <span class="alert-detail">${esc(prettyPeriod(r.period))}</span>
      </div>`).join('')}</div>`;
    $$('.ae-row', el).forEach((row) => on(row, 'click', () => openAirport(row.dataset.iata)));
  } catch { el.innerHTML = '<div class="empty">could not load intensity</div>'; }
}

async function drawAttribution() {
  const el = $('#attribution');
  el.innerHTML = '<div class="loading">loading…</div>';
  try {
    const d = await api(`/api/v1/operations/attribution?direction=${opsDirection()}&top=12`);
    if (d.detail) { el.innerHTML = `<div class="empty">${esc(d.detail)}</div>`; return; }

    const sign = d.national_growth_pct >= 0 ? '+' : '';
    $('#attr-sub').innerHTML = `National freight went from <strong>${n0(d.national_then_mt)}</strong> to `
      + `<strong>${n0(d.national_now_mt)}</strong> MT between ${esc(prettyPeriod(d.period_then))} and `
      + `${esc(prettyPeriod(d.period_now))}: ${sign}${d.national_growth_pct.toFixed(2)}%. `
      + `Each airport below is shown by how much of that it accounts for.`;

    // Scale bars against the largest absolute contribution so the biggest
    // mover fills the track and the rest are readable relative to it.
    const span = Math.max(...d.contributors.map((c) => Math.abs(c.contribution_pp)), 0.01);
    el.innerHTML = `<div class="rows">${d.contributors.map((c) => {
      const pos = c.contribution_pp >= 0;
      const w = (Math.abs(c.contribution_pp) / span) * 50;
      return `<div class="row contrib-row clickable" data-iata="${esc(c.entity_key)}">
        <span class="code">${esc(c.entity_key)}</span>
        <span class="name"><span class="name-main">${esc(c.entity_name)}</span></span>
        <span class="num">${c.change_mt >= 0 ? '+' : ''}${n0(c.change_mt)} MT</span>
        <span class="contrib-track">
          <span class="contrib-zero" style="left:50%"></span>
          <span class="contrib-fill ${pos ? 'pos' : 'neg'}"
                style="${pos ? `left:50%;width:${w}%` : `right:50%;width:${w}%`}"></span>
        </span>
        <span class="delta ${pos ? 'up' : 'down'}">${pos ? '+' : ''}${c.contribution_pp.toFixed(2)} pp</span>
      </div>`;
    }).join('')}</div>`;
    $$('.contrib-row', el).forEach((r) => on(r, 'click', () => r.dataset.iata && openAirport(r.dataset.iata)));
  } catch { el.innerHTML = '<div class="empty">could not load attribution</div>'; }
}

async function drawConcentration() {
  const el = $('#concentration');
  try {
    const d = await api(`/api/v1/operations/concentration?direction=${opsDirection()}`);
    const rows = d.rows.filter((r) => r.hhi);
    if (!rows.length) { el.innerHTML = '<div class="empty">not enough periods</div>'; return; }
    const last = rows[rows.length - 1], first = rows[0];
    const dir = last.hhi < first.hhi ? 'spreading across more airports'
              : last.hhi > first.hhi ? 'consolidating onto fewer airports'
              : 'holding steady';

    const W = 460, H = 130, pad = { l: 40, r: 12, t: 12, b: 26 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
    const vals = rows.map((r) => r.hhi);
    const lo = Math.min(...vals) * 0.96, hi = Math.max(...vals) * 1.04;
    const x = (i) => pad.l + (rows.length < 2 ? iw / 2 : (i / (rows.length - 1)) * iw);
    const y = (v) => pad.t + ih - ((v - lo) / (hi - lo || 1)) * ih;
    const path = rows.map((r, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(r.hhi).toFixed(1)}`).join('');

    el.innerHTML = `
      <p class="hhi-note">Currently <strong>${int(last.hhi)}</strong>:
        ${esc(last.interpretation)}, equivalent to about
        <strong>${last.effective_n}</strong> equally sized airports. The largest handles
        <strong>${last.top_share_pct}%</strong> of all freight. Over these
        ${rows.length} months the market is <strong>${dir}</strong>.</p>
      <div class="chart-wrap"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Concentration over time">
        ${[lo, (lo + hi) / 2, hi].map((v) => `<line class="grid-line" x1="${pad.l}" y1="${y(v)}" x2="${W - pad.r}" y2="${y(v)}"/>
          <text class="ax-text" x="${pad.l - 6}" y="${y(v) + 3}" text-anchor="end">${Math.round(v)}</text>`).join('')}
        <path class="line-hist" d="${path}"/>
        ${rows.map((r, i) => `<circle class="pt-hist" cx="${x(i)}" cy="${y(r.hhi)}" r="2.6"/>`).join('')}
        <text class="ax-text" x="${pad.l}" y="${H - 8}">${esc(prettyPeriod(first.period))}</text>
        <text class="ax-text" x="${W - pad.r}" y="${H - 8}" text-anchor="end">${esc(prettyPeriod(last.period))}</text>
      </svg></div>
      <p class="hhi-note" style="margin-top:10px;color:var(--muted);font-size:12px">
        Herfindahl-Hirschman Index: the sum of squared market shares. Below 1,500 is
        considered unconcentrated, above 2,500 highly concentrated.</p>`;
  } catch { el.innerHTML = '<div class="empty">could not load concentration</div>'; }
}

async function drawBelly() {
  const el = $('#belly');
  try {
    const d = await api('/api/v1/operations/belly-dependency');
    const rows = d.rows.slice(0, 9);
    if (!rows.length) { el.innerHTML = '<div class="empty">no carrier data</div>'; return; }
    el.innerHTML = `<div class="rows">${rows.map((r) => {
      const freighter = r.kind === 'freighter';
      return `<div class="row src-row">
        <span class="pill ${freighter ? 'ok' : ''}">${freighter ? 'freighter' : 'belly'}</span>
        <span class="name"><span class="name-main">${esc(r.entity_key)}</span>
          <div class="alert-detail">${esc(r.reading)} · ${r.months} months</div></span>
        <span class="num">${r.mean_tonnes_per_departure == null ? 'n/a' : r.mean_tonnes_per_departure + ' t/dep'}</span>
      </div>`;
    }).join('')}</div>`;
  } catch { el.innerHTML = '<div class="empty">could not load</div>'; }
}

async function drawEfficiency() {
  const el = $('#efficiency');
  el.innerHTML = '<div class="loading">loading…</div>';
  try {
    const d = await api(`/api/v1/operations/efficiency?limit=40&direction=${opsDirection()}`);
    if (!d.rows.length) { el.innerHTML = '<div class="empty">no paired capacity data</div>'; return; }
    el.innerHTML = `<div class="rows">${d.rows.map((r) => `
      <div class="row eff-row">
        <span class="name"><span class="name-main">${esc(r.entity_key)}</span>
          <div class="alert-detail">${esc(prettyPeriod(r.period))} · FTK ${n0(r.ftk_million)} of ${n0(r.atk_million)} available</div>
          <div class="lf-bar"><span style="width:${Math.min(100, Number(r.cargo_load_factor_pct))}%"></span></div></span>
        <span class="num">${Number(r.cargo_load_factor_pct).toFixed(1)}%</span>
        <span class="num">${r.tonnes_per_departure == null ? 'n/a' : Number(r.tonnes_per_departure).toFixed(2)} t</span>
        <span class="num">${r.mail_share_pct == null ? 'n/a' : Number(r.mail_share_pct).toFixed(1) + '%'}</span>
      </div>`).join('')}</div>`;
    $('#eff-foot').innerHTML = 'Columns: load factor · tonnes per departure · mail share. '
      + 'A carrier lifting 20 tonnes a departure is flying freighters; one lifting under a tonne is selling belly space.';
  } catch { el.innerHTML = '<div class="empty">could not load efficiency</div>'; }
}

async function drawDecomposition(iata, direction) {
  const el = $('#decomposition');
  if (!el) return;
  el.innerHTML = '<div class="loading">…</div>';
  try {
    const d = await api(`/api/v1/operations/growth-decomposition?iata=${encodeURIComponent(iata)}&direction=${direction}`);
    if (d.detail) { el.innerHTML = `<div class="empty">${esc(d.detail)}</div>`; return; }

    // Both effects share one scale so the longer bar is the larger cause,
    // which is the whole point of splitting them.
    const span = Math.max(Math.abs(d.more_flights_mt), Math.abs(d.fuller_flights_mt), 1);
    const bar = (label, value, note) => {
      const pos = value >= 0;
      const w = (Math.abs(value) / span) * 50;
      return `<div class="decomp-bar">
        <span class="decomp-label">${esc(label)}</span>
        <span class="decomp-track"><span class="decomp-mid"></span>
          <span class="decomp-fill ${pos ? 'pos' : 'neg'}"
                style="${pos ? `left:50%;width:${w}%` : `right:50%;width:${w}%`}"></span></span>
        <span class="decomp-val ${pos ? 'up' : 'down'}" style="color:${pos ? 'var(--teal)' : 'var(--rose)'}">
          ${pos ? '+' : ''}${n0(value)} MT</span>
      </div>
      <p class="decomp-note" style="margin:-6px 0 0 140px">${esc(note)}</p>`;
    };

    const dirWord = d.change_mt >= 0 ? 'rose' : 'fell';
    el.innerHTML = `<div class="decomp">
      <p class="decomp-head">Between ${esc(prettyPeriod(d.period_then))} and ${esc(prettyPeriod(d.period_now))},
        freight ${dirWord} from <strong>${n0(d.freight_then_mt)}</strong> to
        <strong>${n0(d.freight_now_mt)}</strong> MT,
        ${d.change_pct >= 0 ? '+' : ''}${d.change_pct}%. That splits into:</p>
      ${bar('More flights', d.more_flights_mt,
            `${n0(d.flights_then)} → ${n0(d.flights_now)} flights`)}
      ${bar('Fuller flights', d.fuller_flights_mt,
            `${d.tonnes_per_flight_then} → ${d.tonnes_per_flight_now} tonnes per flight`)}
      <p class="decomp-head" style="margin-top:4px">The larger cause was
        <strong>${esc(d.driver)}</strong>.</p>
    </div>`;
  } catch { el.innerHTML = '<div class="empty">could not decompose this series</div>'; }
}

/* ----------------------------------------------------------- forecasts */

async function loadForecasts() {
  const el = $('#forecasts');
  el.innerHTML = '<div class="loading">loading…</div>';
  try {
    const d = await api(`/api/v1/forecasts?limit=60&grain=${$('#fc-grain').value}&horizon=${$('#fc-horizon').value}`);
    $('#nav-forecast-count').textContent = d.row_count ? int(d.row_count) : '';

    const mapes = d.rows.map((r) => Number(r.backtest_mape_pct)).filter(Number.isFinite).sort((a, b) => a - b);
    const median = mapes.length ? mapes[Math.floor(mapes.length / 2)] : null;
    const models = [...new Set(d.rows.map((r) => r.model))];
    $('#forecast-kpis').innerHTML = [
      ['Series projected', int(d.row_count), 'at this horizon', true],
      ['Typical error', median == null ? 'n/a' : median.toFixed(1) + '%', 'median backtest MAPE', false],
      ['Models used', models.length, models.join(', ') || 'n/a', false],
      ['Interval', '80%', 'four times in five', false],
    ].map(([l, v, note, a]) => `<div class="kpi${a ? ' accent' : ''}">
        <div class="k-label">${l}</div><div class="k-value">${v}</div><div class="k-note">${esc(note)}</div></div>`).join('');

    if (!d.rows.length) return void (el.innerHTML = '<div class="empty">no forecasts at this horizon</div>');
    el.innerHTML = `<div class="rows">${d.rows.map((r) => {
      const mape = Number(r.backtest_mape_pct);
      const good = Number.isFinite(mape) && mape <= 12;
      return `<div class="row fc-row clickable" data-iata="${esc(r.grain === 'AIRPORT' ? r.entity_key : '')}">
        <span class="code">${esc(r.entity_key)}</span>
        <span class="name"><span class="name-main">${esc(r.entity_name || r.entity_key)}</span>
          <div class="alert-detail">${esc(prettyPeriod(r.period))} · ${esc(String(r.direction).toLowerCase())} · ${esc(r.model)}</div>
        </span>
        <span class="num">${n1(r.predicted_mt)} <span class="unit" style="color:var(--muted)">MT</span></span>
        <span class="alert-detail">${n0(r.lower_mt)} to ${n0(r.upper_mt)}</span>
        <span class="pill ${good ? 'ok' : 'warn'}" title="Past predictions were typically within this much of the truth">±${Number.isFinite(mape) ? mape.toFixed(1) : '?'}%</span>
      </div>`;
    }).join('')}</div>`;
    $$('.fc-row', el).forEach((row) => on(row, 'click', () => {
      if (row.dataset.iata) openAirport(row.dataset.iata);
    }));
  } catch { el.innerHTML = '<div class="empty">could not load forecasts</div>'; }
}

/* -------------------------------------------------------------- alerts */

async function loadAlerts() {
  const el = $('#alerts');
  el.innerHTML = '<div class="loading">loading…</div>';
  try {
    const g = $('#alert-grain').value;
    const d = await api(`/api/v1/anomalies?limit=30${g ? `&grain=${g}` : ''}`);
    $('#nav-alert-count').textContent = d.rows.length ? d.rows.length : '';
    $('#alert-sub').textContent = `${d.rows.length} flagged, most significant first.`;
    if (!d.rows.length) return void (el.innerHTML = '<div class="empty">nothing flagged</div>');
    el.innerHTML = d.rows.map((r) => {
      const sev = String(r.severity || '').toLowerCase();
      const cls = sev === 'high' ? 'high' : sev === 'medium' ? 'med' : 'low';
      return `<div class="alert">
        <span class="alert-sev ${cls}"></span>
        <div class="alert-body">
          <div class="alert-top">
            <span class="alert-name">${esc(r.entity_name || r.entity_key)}</span>
            <span class="code">${esc(r.entity_key)}</span>
            <span class="pill ${cls === 'high' ? 'bad' : 'warn'}">${esc(r.severity)}</span>
            <span class="pill">${esc(methodLabel(r.method))}</span>
          </div>
          <div class="alert-plain">${esc(plainAnomaly(r))}</div>
          <div class="alert-detail">observed ${n1(r.observed_mt)} · expected ${n1(r.expected_mt)} · ${esc(String(r.direction).toLowerCase())}</div>
        </div></div>`;
    }).join('');
  } catch { el.innerHTML = '<div class="empty">could not load alerts</div>'; }
}

async function loadInsights() {
  const el = $('#insights');
  try {
    const d = await api('/api/v1/insights?limit=12');
    el.innerHTML = d.rows.length
      ? d.rows.map((r) => `<div class="insight"><h3>${esc(r.headline)}</h3><p>${esc(r.narrative)}</p></div>`).join('')
      : '<div class="empty">No explanations written yet. Run the insight stage.</div>';
  } catch { el.innerHTML = '<div class="empty">could not load explanations</div>'; }
}

/* ------------------------------------------------------- agent console */

const policyClass = (p = '') =>
  p.startsWith('llm') || p === 'model' ? 'model'
  : p.startsWith('heuristic(fallback') ? 'fallback'
  : p.startsWith('heuristic') ? 'heuristic' : '';

async function loadAgents() {
  try {
    const s = await api('/api/v1/agents/stats');
    const t = s.totals || {};
    $('#agent-totals').innerHTML = [
      ['Runs', int(t.runs)], ['Steps', int(t.steps)], ['Pipelines', int(t.traces)],
      ['Succeeded', int(t.succeeded)], ['Model tokens', int(t.tokens || 0)],
    ].map(([l, v]) => `<div class="stat"><div class="s-value">${v}</div><div class="s-label">${l}</div></div>`).join('');
    $('#nav-agent-count').textContent = int(t.runs);

    const maxCalls = Math.max(...s.tools.map((x) => x.calls), 1);
    $('#tool-stats').innerHTML = `<div class="rows">${s.tools.map((x) => `
      <div class="row tool-row">
        <span class="name"><span class="step-tool">${esc(x.tool)}</span>
          <div class="bar-wrap"><div class="bar" style="width:${(x.calls / maxCalls) * 100}%"></div></div></span>
        <span class="num">${int(x.calls)}</span>
        <span class="pill ${x.calls > x.runs_using ? 'warn' : 'ok'}">${x.calls > x.runs_using ? `${x.calls - x.runs_using} retried` : 'no retry'}</span>
      </div>`).join('')}</div>`;

    const colours = { model: 'var(--violet)', heuristic: 'var(--indigo)', mixed: 'var(--saffron)', unrecorded: 'var(--faint)', none: 'var(--faint)' };
    const total = s.policies.reduce((a, p) => a + Number(p.runs), 0) || 1;
    $('#policy-stats').innerHTML = `
      <div class="policy-bar">${s.policies.map((p) =>
        `<div style="width:${(p.runs / total) * 100}%;background:${colours[p.effective_policy] || 'var(--faint)'}"></div>`).join('')}</div>
      <div class="legend">${s.policies.map((p) => `<div class="legend-item">
          <span class="legend-swatch" style="background:${colours[p.effective_policy] || 'var(--faint)'}"></span>
          <span>${esc(p.effective_policy)}</span><span class="num">${int(p.runs)} runs</span></div>`).join('')}
        ${s.policies.some((p) => p.effective_policy === 'unrecorded')
          ? '<div class="legend-item" style="color:var(--muted);font-size:12px;margin-top:5px">“unrecorded” predates per-step policy tracking. Those runs are not claimed as model decisions.</div>' : ''}
      </div>`;

    $('#run-agent').innerHTML = '<option value="">All agents</option>'
      + s.agents.map((a) => `<option value="${esc(a.agent)}">${esc(a.agent)}</option>`).join('');
  } catch { $('#agent-totals').innerHTML = '<div class="empty">agent stats unavailable</div>'; }
  loadRuns();
}

async function loadRuns() {
  const el = $('#run-list');
  el.innerHTML = '<div class="loading">loading…</div>';
  try {
    const agent = $('#run-agent').value;
    const d = await api(`/api/v1/agents/runs?limit=40${agent ? `&agent=${encodeURIComponent(agent)}` : ''}`);
    if (!d.rows.length) return void (el.innerHTML = '<div class="empty">no recorded runs</div>');
    el.innerHTML = `<div class="rows">${d.rows.map((r) => `
      <div class="row run-row" data-run="${r.agent_run_id}">
        <span class="pill ${r.succeeded ? 'ok' : 'bad'}">${r.succeeded ? 'met' : 'gave up'}</span>
        <span class="name"><span class="name-main"><strong>${esc(r.agent)}</strong>: ${esc(r.goal)}</span></span>
        <span class="pill ${policyClass(r.effective_policy)}">${esc(r.effective_policy)}</span>
        <span class="num">${r.steps} steps</span>
        <span class="num">${int(r.elapsed_ms)} ms</span>
      </div>`).join('')}</div>`;
    $$('.run-row', el).forEach((row) => on(row, 'click', () => {
      $$('.run-row', el).forEach((x) => x.classList.remove('is-selected'));
      row.classList.add('is-selected');
      $('#replay-run').value = row.dataset.run;
      showRun(row.dataset.run);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }));
    $('#replay-run').innerHTML = d.rows.map((r) =>
      `<option value="${r.agent_run_id}">#${r.agent_run_id} · ${esc(r.agent)} · ${r.steps} steps</option>`).join('');
    showRun(d.rows[0].agent_run_id);
  } catch { el.innerHTML = '<div class="empty">could not load runs</div>'; }
}

function renderGoal(run, note = '') {
  $('#replay-goal').innerHTML = `
    <div class="goal-line">
      <span class="pill ${run.succeeded ? 'ok' : 'bad'}">${run.succeeded ? 'goal met' : 'gave up'}</span>
      <span class="pill">${esc(run.agent)}</span>
      <span class="pill ${policyClass(run.effective_policy)}">${esc(run.effective_policy)}</span>
      <span class="step-ms">${int(run.elapsed_ms)} ms</span>
    </div>
    <p class="goal-text">${esc(run.goal)}</p>
    ${note ? `<p class="step-ms">${esc(note)}</p>` : ''}`;
}

const stepHTML = (s) => {
  const args = s.args && Object.keys(s.args).length ? JSON.stringify(s.args) : '';
  return `<li class="step ${s.ok ? 'ok' : 'err'}">
    <span class="step-dot">${s.ok ? '✓' : '!'}</span>
    <div class="step-main">
      <div class="step-top"><span class="step-tool">${esc(s.tool)}</span>
        ${s.policy ? `<span class="pill ${policyClass(s.policy)}">${esc(s.policy.replace(/^llm:/, ''))}</span>` : ''}
        <span class="step-ms">${int(s.elapsed_ms)} ms</span></div>
      ${s.reasoning ? `<p class="step-why">${esc(s.reasoning)}</p>` : ''}
      ${args ? `<div class="step-args">${esc(args.slice(0, 260))}</div>` : ''}
      <div class="step-obs">${esc(String(s.observation).slice(0, 340))}</div>
    </div></li>`;
};

async function showRun(id) {
  const list = $('#replay-steps');
  list.innerHTML = '';
  try {
    const d = await api(`/api/v1/agents/runs/${id}`);
    renderGoal(d.run);
    list.innerHTML = d.steps.map(stepHTML).join('');
    $('#replay-foot').textContent = d.run.result_summary || '';
  } catch { list.innerHTML = '<div class="empty">could not load trace</div>'; }
}

let stream = null;
function replay() {
  const id = $('#replay-run').value;
  if (!id) return;
  if (stream) { stream.close(); stream = null; }
  const btn = $('#replay-btn'), list = $('#replay-steps');
  list.innerHTML = ''; btn.disabled = true; btn.textContent = '● Replaying';
  stream = new EventSource(`/api/v1/agents/stream?run_id=${id}&delay_ms=460`);
  stream.addEventListener('run', (e) => renderGoal(JSON.parse(e.data), 'recorded trace, replayed at reading pace'));
  stream.addEventListener('step', (e) => {
    list.insertAdjacentHTML('beforeend', stepHTML(JSON.parse(e.data)));
    list.lastElementChild.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
  const finish = (t) => {
    if (stream) { stream.close(); stream = null; }
    btn.disabled = false; btn.textContent = '▶ Replay'; $('#replay-foot').textContent = t;
  };
  stream.addEventListener('done', (e) => {
    const d = JSON.parse(e.data);
    finish(`${d.steps} steps · ${d.summary || (d.succeeded ? 'goal met' : 'gave up')}`);
  });
  // EventSource reconnects by default; a finished stream closes server-side
  // and would otherwise replay forever.
  stream.onerror = () => finish('stream ended');
}

/* ----------------------------------------------------------------- ask */

const SUGGESTIONS = [
  'Which airports handle the most cargo?',
  'Which airlines carry the most cargo?',
  'Show the fastest growing airports',
  'What anomalies were detected?',
  'Where does the data come from?',
];

function bubble(html, who = 'bot') {
  const div = document.createElement('div');
  div.className = `msg ${who}`;
  div.innerHTML = html;
  $('#chat-log').append(div);
  div.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  return div;
}

async function ask(question) {
  bubble(`<p>${esc(question)}</p>`, 'user');
  const thinking = bubble('<p class="step-ms">querying the semantic layer…</p>');
  try {
    const d = await api('/api/v1/chat/query', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });
    const cols = d.rows.length ? Object.keys(d.rows[0]) : [];
    const table = d.rows.length ? `<table class="answer-table">
        <tr>${cols.map((c) => `<th>${esc(c.replace(/_/g, ' '))}</th>`).join('')}</tr>
        ${d.rows.slice(0, 6).map((r) => `<tr>${cols.map((c) => {
          const v = r[c];
          const num = v !== null && v !== '' && !Number.isNaN(Number(v));
          return `<td class="${num ? 'n' : ''}">${num ? n1(v) : esc(v)}</td>`;
        }).join('')}</tr>`).join('')}</table>` : '';
    const passages = (d.passages || []).slice(0, 2).map((p) => `<div class="passage">
        <div class="p-src">${esc(p.publisher)}${p.page ? ` · page ${p.page}` : ''}</div>
        ${esc(String(p.excerpt).slice(0, 190))}…</div>`).join('');
    const cites = (d.citations || []).slice(0, 3).map((c) =>
      `<a href="${esc(c.source_url)}" target="_blank" rel="noopener">↗ ${esc(c.publisher)}: ${esc(c.title || c.source_url)}</a>`).join('');
    thinking.innerHTML = `
      <div class="meta"><span class="pill ${d.grounded ? 'ok' : 'bad'}">${d.grounded ? 'every figure traced' : 'ungrounded'}</span>
        <span class="pill">${esc(d.intent)}</span></div>
      <p>${esc(d.answer)}</p>${table}
      ${passages ? `<div style="margin-top:9px"><div class="p-src">retrieved passages</div>${passages}</div>` : ''}
      ${cites ? `<div class="cites">${cites}</div>` : ''}
      <p class="step-ms" style="margin-top:8px">read as: ${esc(d.understood_as)}</p>`;
  } catch (e) { thinking.innerHTML = `<p>Could not answer that (${esc(e.message)}).</p>`; }
}

/* --------------------------------------------------------------- brief */

async function loadBrief() {
  const el = $('#brief');
  try {
    const res = await fetch('/api/v1/reports/brief');
    const html = await res.text();
    // The endpoint returns a whole document; lift its body so it inherits
    // the dashboard's theme instead of fighting it.
    const body = /<body[^>]*>([\s\S]*)<\/body>/i.exec(html);
    el.innerHTML = body ? body[1] : html;
    el.querySelectorAll('script, link, style').forEach((n) => n.remove());
  } catch { el.innerHTML = '<div class="empty">could not load the brief</div>'; }
}

/* ---------------------------------------------------- sources & health */

async function loadSources() {}

async function loadPublishers() {
  try {
    const d = await api('/api/v1/sources');
    $('#sources').innerHTML = `<div class="rows">${d.publishers.map((p) => `
      <div class="row src-row"><span class="code">${esc(p.publisher)}</span>
        <span class="name"><span class="name-main">${int(p.documents)} documents</span>
          <div class="alert-detail">retrieved ${esc(ago(p.last_retrieved))}</div></span>
        <span class="num">${int(p.facts)} facts</span></div>`).join('')}</div>`;
  } catch { $('#sources').innerHTML = '<div class="empty">could not load sources</div>'; }

  try {
    const s = await api('/api/v1/search/stats');
    $('#index-stats').innerHTML = `<div class="rows">
      ${s.by_publisher.map((p) => `<div class="row src-row"><span class="code">${esc(p.publisher)}</span>
        <span class="name"><span class="name-main">${int(p.documents)} documents indexed</span></span>
        <span class="num">${int(p.chunks)} passages</span></div>`).join('')}
      <div class="row src-row"><span class="code">MODEL</span>
        <span class="name"><span class="name-main">${esc(s.embed_models.join(', ') || 'none')}</span></span>
        <span class="num">${int(s.vocabulary_terms)} terms</span></div></div>`;
  } catch { $('#index-stats').innerHTML = '<div class="empty">index unavailable</div>'; }
}

async function loadHealth() {
  const el = $('#health');
  let html = '';
  try {
    const p = await api('/api/v1/pipeline/state');
    html += p.stages?.length
      ? `<div>${p.stages.map((s) => `<div class="stage-row">
            <span class="pill ${s.ok ? 'ok' : 'bad'}">${s.ok ? 'ok' : 'failed'}</span>
            <span class="stage-name">${esc(s.name)}</span>
            <span class="stage-detail">${esc(String(s.detail).slice(0, 90))}</span>
            <span class="stage-secs">${Number(s.seconds).toFixed(1)}s</span></div>`).join('')}</div>`
      : `<p class="empty" style="text-align:left;padding:0">${esc(p.detail || 'no run recorded')}</p>`;
  } catch { html += '<p class="empty" style="text-align:left;padding:0">pipeline state unavailable</p>'; }

  try {
    const text = await (await fetch('/metrics')).text();
    const want = /^(aci_[a-z_]+)(?:\{[^}]*\})?\s+([0-9.e+-]+)$/gim;
    const seen = new Map();
    let m;
    while ((m = want.exec(text))) if (!seen.has(m[1])) seen.set(m[1], Number(m[2]));
    if (seen.size) {
      html += `<div class="metric-grid">${[...seen].slice(0, 8).map(([k, v]) =>
        `<div class="metric"><div class="m-name">${esc(k.replace(/^aci_/, ''))}</div>
         <div class="m-val">${Number.isInteger(v) ? int(v) : n1(v)}</div></div>`).join('')}</div>`;
    }
  } catch { /* metrics are optional context */ }
  el.innerHTML = html || '<div class="empty">no health data</div>';
}

/* ---------------------------------------------------------------- boot */

const LOADERS = {
  overview: loadOverview, airport: loadAirportView, operations: loadOperations,
  forecast: loadForecasts,
  alerts: loadAlerts, ask: () => {}, brief: loadBrief,
  agents: loadAgents, sources: loadSources,
};

async function loadPipelineChip() {
  try {
    const p = await api('/api/v1/pipeline/state');
    const chip = $('#pipeline-chip');
    if (p.stages?.length) {
      chip.classList.add(p.ok ? 'ok' : 'bad');
      $('#pipeline-label').textContent = p.ok ? `pipeline ok · ${ago(p.finished_at)}` : 'last run failed';
      $('#freshness').textContent = `data refreshed ${ago(p.finished_at)}`;
    } else {
      $('#pipeline-label').textContent = 'no run recorded yet';
      $('#freshness').textContent = '';
    }
  } catch {
    $('#pipeline-label').textContent = 'pipeline unknown';
    $('#freshness').textContent = '';
  }
}

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const lbl = $('#theme-label');
  if (lbl) lbl.textContent = t === 'dark' ? 'Light mode' : 'Dark mode';
  try { localStorage.setItem('aci-theme', t); } catch { /* private mode */ }
}

function init() {
  let saved = null;
  try { saved = localStorage.getItem('aci-theme'); } catch { /* private mode */ }
  applyTheme(saved || 'light');
  on('#theme-toggle', 'click', () =>
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));

  const rail = $('#rail'), scrim = $('#scrim');
  const closeRail = () => { rail?.classList.remove('is-open'); if (scrim) scrim.hidden = true; };
  on('#menu-btn', 'click', () => {
    rail?.classList.toggle('is-open');
    if (scrim) scrim.hidden = !rail?.classList.contains('is-open');
  });
  on(scrim, 'click', closeRail);
  $$('.nav-item').forEach((b) => on(b, 'click', () => { show(b.dataset.view); closeRail(); }));

  $$('.subtabs').forEach((nav) => {
    const view = nav.dataset.for;
    $$('.subtab', nav).forEach((btn) =>
      on(btn, 'click', () => selectTab(view, btn.dataset.tab)));
  });

  on('#rank-direction', 'change', loadRankings);
  on('#rank-order', 'change', loadRankings);
  on('#airport-pick', 'change', (e) => drawAirport(e.target.value));
  on('#airport-direction', 'change', () => drawAirport($('#airport-pick').value));
  // View-level controls redraw the visible tab and mark the rest stale, so
  // a tab opened later reflects the current selection rather than whatever
  // was chosen when it first loaded.
  on('#ops-direction', 'change', () => refreshView('operations'));
  on('#airport-direction', 'change', () => {
    invalidateTabs('airport');
    drawAirport($('#airport-pick').value);
  });
  on('#fc-grain', 'change', loadForecasts);
  on('#fc-horizon', 'change', loadForecasts);
  on('#alert-grain', 'change', loadAlerts);
  on('#run-agent', 'change', loadRuns);
  on('#replay-btn', 'click', replay);
  on('#replay-run', 'change', (e) => showRun(e.target.value));

  const sug = $('#suggestions');
  if (sug) {
    sug.innerHTML = SUGGESTIONS.map((s) => `<button class="chip">${esc(s)}</button>`).join('');
    $$('.chip', sug).forEach((c) => on(c, 'click', () => ask(c.textContent)));
  }
  bubble('<p>Ask about airport rankings, airline share, anomalies, forecasts or sources. '
       + 'Every figure comes from a stored row and carries the document behind it.</p>');

  on('#chat-form', 'submit', (e) => {
    e.preventDefault();
    const v = $('#chat-input').value.trim();
    if (!v) return;
    $('#chat-input').value = '';
    ask(v);
  });
  on('#search-form', 'submit', (e) => {
    e.preventDefault();
    const v = $('#search-input').value.trim();
    if (v) runSearch(v);
  });
  on(window, 'hashchange', () => show(location.hash.slice(1)));

  loadPipelineChip();
  // Alert and forecast counts are wanted in the sidebar before those views
  // are opened, so fetch just the counts up front.
  api('/api/v1/anomalies?limit=30').then((d) => { $('#nav-alert-count').textContent = d.rows.length || ''; }).catch(() => {});
  api('/api/v1/agents/stats').then((s) => { $('#nav-agent-count').textContent = int(s.totals?.runs || 0); }).catch(() => {});

  show(location.hash.slice(1) || 'overview');
}

async function runSearch(q) {
  const el = $('#search-results');
  el.innerHTML = '<div class="loading">searching…</div>';
  try {
    const d = await api(`/api/v1/search?q=${encodeURIComponent(q)}&top_k=6`);
    el.innerHTML = d.passages.length ? d.passages.map((p) => `<div class="result">
        <div class="result-head"><span class="code">${esc(p.publisher)}</span>
          ${p.page ? `<span class="pill">page ${p.page}</span>` : ''}
          <a href="${esc(p.source_url)}" target="_blank" rel="noopener">↗ document</a></div>
        <div class="result-body">${esc(String(p.excerpt).slice(0, 300))}…</div></div>`).join('')
      : '<div class="empty">nothing matched</div>';
  } catch { el.innerHTML = '<div class="empty">search failed</div>'; }
}

document.addEventListener('DOMContentLoaded', () => {
  try { init(); } catch (e) { console.error('init failed', e); try { show('overview'); } catch { /* noop */ } }
});
