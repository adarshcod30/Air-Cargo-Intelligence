/* Air Cargo Intelligence — dashboard.

   No framework and no build step: the API serves this directory directly,
   so there is nothing to compile before the project runs. Every figure
   rendered here arrives from the API already computed; this file formats
   and lays out, it never calculates a cargo number.                       */

'use strict';

const $  = (s, r = document) => r.querySelector(s);

/* Bind only if the element exists.

   init() used to call addEventListener directly on every lookup, so a
   single missing node threw and every loader after it never ran - the page
   rendered its chrome and then sat on "loading…" forever. That is a real
   deployment risk, not just a local one: a cached index.html served
   alongside a fresh app.js produces exactly that mismatch. Degrading one
   control is recoverable; losing the whole page is not.                  */
const on = (sel, event, fn) => {
  const el = typeof sel === 'string' ? $(sel) : sel;
  if (el) el.addEventListener(event, fn);
  return el;
};
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const api = async (path, opts) => {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
};

const num = (v, d = 1) =>
  v === null || v === undefined || v === '' || Number.isNaN(Number(v))
    ? '—'
    : Number(v).toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d });

const int = (v) => (v === null || v === undefined ? '—' : Number(v).toLocaleString('en-IN'));

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const ago = (iso) => {
  if (!iso) return 'unknown';
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 90) return 'just now';
  if (secs < 5400) return `${Math.round(secs / 60)} min ago`;
  if (secs < 172800) return `${Math.round(secs / 3600)} h ago`;
  return `${Math.round(secs / 86400)} d ago`;
};

/* ------------------------------------------------------------ routing -- */

const VIEWS = {
  overview: ['Overview', 'Cargo throughput across Indian and international airports.'],
  agents:   ['Agent console', 'What each agent decided, why, and which policy decided it.'],
  intel:    ['Intelligence', 'Detected anomalies, forecasts and written explanations.'],
  ask:      ['Ask the data', 'Questions answered by SQL over a governed semantic layer.'],
  sources:  ['Provenance', 'Every figure traces to a published document.'],
};

const loaded = new Set();

function show(view) {
  $$('.nav-item').forEach((b) => b.classList.toggle('is-active', b.dataset.view === view));
  $$('.view').forEach((s) => s.classList.toggle('is-active', s.dataset.view === view));
  const [title, sub] = VIEWS[view] || VIEWS.overview;
  $('#view-title').textContent = title;
  $('#view-sub').textContent = sub;
  location.hash = view;

  // Panels fetch on first reveal rather than all at once on load: the agent
  // console alone is four queries, and most visits never open it.
  if (!loaded.has(view)) { loaded.add(view); (LOADERS[view] || (() => {}))(); }
}

/* ----------------------------------------------------------- overview -- */

async function loadOverview() {
  try {
    const h = await api('/api/v1/health');
    $('#kpis').innerHTML = [
      ['Facts', int(h.facts), 'reconciled rows'],
      ['Airports', int(h.airports), 'resolved to IATA'],
      ['Airlines', int(h.airlines), 'carriers'],
      ['Periods', int(h.periods), 'months and years'],
    ].map(([l, v, n]) => `
      <div class="kpi">
        <div class="k-label">${l}</div>
        <div class="k-value">${v}</div>
        <div class="k-note">${n}</div>
      </div>`).join('');
  } catch { $('#kpis').innerHTML = '<div class="empty">API unreachable</div>'; }

  loadRankings();
  loadAirlines();
}

async function loadRankings() {
  const el = $('#rankings');
  const dir = $('#rank-direction').value;
  const order = $('#rank-order').value;
  el.innerHTML = '<div class="loading">loading…</div>';
  try {
    const d = await api(`/api/v1/airports/rankings?limit=10&direction=${dir}&order=${order}`);
    if (!d.rows.length) { el.innerHTML = '<div class="empty">no rows</div>'; return; }
    const max = Math.max(...d.rows.map((r) => Number(r.tonnage_mt) || 0));
    el.innerHTML = `<div class="rows">${d.rows.map((r, i) => {
      const g = Number(r.growth_yoy_pct);
      const cls = Number.isFinite(g) ? (g >= 0 ? 'up' : 'down') : '';
      const sign = Number.isFinite(g) && g >= 0 ? '+' : '';
      return `<div class="row rank-row">
        <span class="rank">${i + 1}</span>
        <span class="code">${esc(r.airport_iata || '—')}</span>
        <span class="name" title="${esc(r.airport_name)}">${esc(r.airport_name)}
          <div class="bar-wrap"><div class="bar" style="width:${max ? (Number(r.tonnage_mt) / max) * 100 : 0}%"></div></div>
        </span>
        <span class="num">${num(r.tonnage_mt)}</span>
        <span class="delta ${cls}">${Number.isFinite(g) ? sign + g.toFixed(1) + '%' : '—'}</span>
      </div>`;
    }).join('')}</div>`;
    $('#rank-note').textContent = d.explanation ? d.explanation.slice(0, 150) : '';
  } catch { el.innerHTML = '<div class="empty">could not load rankings</div>'; }
}

async function loadAirlines() {
  const el = $('#airline-chart');
  try {
    const d = await api('/api/v1/airlines/share?limit=7');
    if (!d.rows.length) { el.innerHTML = '<div class="empty">no rows</div>'; return; }
    const max = Math.max(...d.rows.map((r) => Number(r.tonnage_mt)));
    const rowH = 30, pad = 4;
    const h = d.rows.length * rowH + pad * 2;
    // Inline SVG rather than a charting library: one bar chart does not
    // justify a dependency, and this keeps the page loading no external code.
    el.innerHTML = `<div class="chart"><svg viewBox="0 0 480 ${h}" role="img"
        aria-label="Airline cargo share">
      ${d.rows.map((r, i) => {
        const y = pad + i * rowH;
        const w = max ? (Number(r.tonnage_mt) / max) * 215 : 0;
        return `
          <text class="b-label" x="0" y="${y + 13}">${esc(String(r.airline_name).slice(0, 18))}</text>
          <rect class="b-rect" x="120" y="${y + 4}" width="${w}" height="13" rx="2.5" opacity="${1 - i * 0.09}"/>
          <text class="b-value" x="${120 + w + 6}" y="${y + 14}">${num(r.tonnage_mt, 0)}</text>`;
      }).join('')}
    </svg></div>`;
  } catch { el.innerHTML = '<div class="empty">could not load chart</div>'; }
}

/* ----------------------------------------------------- agent console -- */

const policyClass = (p = '') =>
  p.startsWith('llm') ? 'model'
  : p.startsWith('heuristic(fallback') ? 'fallback'
  : p.startsWith('heuristic') ? 'heuristic'
  : 'unrecorded';

const policyLabel = (p = '') =>
  p.startsWith('llm') ? p.replace(/^llm:/, '') : p || 'unrecorded';

async function loadAgents() {
  try {
    const s = await api('/api/v1/agents/stats');
    const t = s.totals || {};
    $('#agent-totals').innerHTML = [
      ['Runs', int(t.runs)],
      ['Steps', int(t.steps)],
      ['Pipelines', int(t.traces)],
      ['Succeeded', `${int(t.succeeded)}`],
      ['Model tokens', int(t.tokens || 0)],
    ].map(([l, v]) => `<div class="stat"><div class="s-value">${v}</div><div class="s-label">${l}</div></div>`).join('');
    $('#nav-agent-count').textContent = int(t.runs);

    // calls > runs_using means the agent invoked a tool more than once in a
    // run, which is what recovering from a failed parser looks like.
    const maxCalls = Math.max(...s.tools.map((x) => x.calls), 1);
    $('#tool-stats').innerHTML = `<div class="rows">${s.tools.map((x) => `
      <div class="row tool-row">
        <span class="name"><span class="step-tool">${esc(x.tool)}</span>
          <div class="bar-wrap"><div class="bar" style="width:${(x.calls / maxCalls) * 100}%"></div></div>
        </span>
        <span class="num">${int(x.calls)}</span>
        <span class="pill ${x.failed_calls ? 'bad' : 'ok'}">${x.calls > x.runs_using ? `${x.calls - x.runs_using} retried` : 'no retry'}</span>
      </div>`).join('')}</div>`;

    const colours = { model: 'var(--violet)', heuristic: 'var(--accent)', mixed: 'var(--warn)', unrecorded: 'var(--muted)', none: 'var(--muted)' };
    const total = s.policies.reduce((a, p) => a + Number(p.runs), 0) || 1;
    $('#policy-stats').innerHTML = `
      <div class="policy-bar">${s.policies.map((p) =>
        `<div class="policy-seg" style="width:${(p.runs / total) * 100}%;background:${colours[p.effective_policy] || 'var(--muted)'}"></div>`).join('')}</div>
      <div class="legend">${s.policies.map((p) => `
        <div class="legend-item">
          <span class="legend-swatch" style="background:${colours[p.effective_policy] || 'var(--muted)'}"></span>
          <span>${esc(p.effective_policy)}</span>
          <span class="num">${int(p.runs)} runs</span>
        </div>`).join('')}
        ${s.policies.some((p) => p.effective_policy === 'unrecorded')
          ? '<div class="legend-item" style="color:var(--muted);font-size:11.5px;margin-top:4px">“unrecorded” predates per-step policy tracking — those runs are not claimed as model decisions.</div>'
          : ''}
      </div>`;

    const agents = s.agents.map((a) => a.agent);
    $('#run-agent').innerHTML = '<option value="">All agents</option>' +
      agents.map((a) => `<option value="${esc(a)}">${esc(a)}</option>`).join('');
  } catch { $('#agent-totals').innerHTML = '<div class="empty">agent stats unavailable</div>'; }

  loadRuns();
}

async function loadRuns() {
  const el = $('#run-list');
  const agent = $('#run-agent').value;
  el.innerHTML = '<div class="loading">loading…</div>';
  try {
    const d = await api(`/api/v1/agents/runs?limit=40${agent ? `&agent=${encodeURIComponent(agent)}` : ''}`);
    if (!d.rows.length) { el.innerHTML = '<div class="empty">no recorded runs</div>'; return; }

    el.innerHTML = `<div class="rows">${d.rows.map((r) => `
      <div class="row run-row" data-run="${r.agent_run_id}">
        <span class="pill ${r.succeeded ? 'ok' : 'bad'}">${r.succeeded ? 'ok' : 'failed'}</span>
        <span class="name"><strong>${esc(r.agent)}</strong> — ${esc(r.goal)}</span>
        <span class="pill ${policyClass(r.effective_policy === 'model' ? 'llm' : r.effective_policy)}">${esc(r.effective_policy)}</span>
        <span class="num">${r.steps} steps</span>
        <span class="num">${int(r.elapsed_ms)} ms</span>
      </div>`).join('')}</div>`;

    $$('.run-row', el).forEach((row) =>
      row.addEventListener('click', () => {
        $$('.run-row', el).forEach((x) => x.classList.remove('is-selected'));
        row.classList.add('is-selected');
        $('#replay-run').value = row.dataset.run;
        showRun(row.dataset.run);
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }));

    const sel = $('#replay-run');
    sel.innerHTML = d.rows.map((r) =>
      `<option value="${r.agent_run_id}">#${r.agent_run_id} · ${esc(r.agent)} · ${r.steps} steps</option>`).join('');
    if (d.rows.length) showRun(d.rows[0].agent_run_id);
  } catch { el.innerHTML = '<div class="empty">could not load runs</div>'; }
}

function renderGoal(run, note = '') {
  $('#replay-goal').innerHTML = `
    <div class="goal-line">
      <span class="pill ${run.succeeded ? 'ok' : 'bad'}">${run.succeeded ? 'goal met' : 'gave up'}</span>
      <span class="pill">${esc(run.agent)}</span>
      <span class="pill ${policyClass(run.effective_policy === 'model' ? 'llm' : run.effective_policy)}">${esc(run.effective_policy)}</span>
      <span class="step-ms">${int(run.elapsed_ms)} ms</span>
    </div>
    <p class="goal-text">${esc(run.goal)}</p>
    ${note ? `<p class="step-ms">${esc(note)}</p>` : ''}`;
}

function stepHTML(s) {
  const args = s.args && Object.keys(s.args).length ? JSON.stringify(s.args) : '';
  return `<li class="step ${s.ok ? 'ok' : 'err'}">
    <span class="step-dot">${s.ok ? '✓' : '!'}</span>
    <div class="step-main">
      <div class="step-top">
        <span class="step-tool">${esc(s.tool)}</span>
        ${s.policy ? `<span class="pill ${policyClass(s.policy)}">${esc(policyLabel(s.policy))}</span>` : ''}
        <span class="step-ms">${int(s.elapsed_ms)} ms</span>
      </div>
      ${s.reasoning
        ? `<p class="step-why">${esc(s.reasoning)}</p>`
        : (s.policy ? '' : '<p class="step-why" style="opacity:.5;border-color:var(--border)">reasoning not recorded — this run predates per-step provenance</p>')}
      ${args ? `<div class="step-args">${esc(args.slice(0, 260))}</div>` : ''}
      <div class="step-obs">${esc(String(s.observation).slice(0, 340))}</div>
    </div>
  </li>`;
}

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

  const btn = $('#replay-btn');
  const list = $('#replay-steps');
  list.innerHTML = '';
  btn.disabled = true;
  btn.textContent = '● Replaying';
  $('#replay-foot').textContent = 'streaming…';

  stream = new EventSource(`/api/v1/agents/stream?run_id=${id}&delay_ms=460`);

  stream.addEventListener('run', (e) =>
    renderGoal(JSON.parse(e.data), 'replay of a recorded trace, paced for reading'));

  stream.addEventListener('step', (e) => {
    list.insertAdjacentHTML('beforeend', stepHTML(JSON.parse(e.data)));
    list.lastElementChild.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });

  const finish = (text) => {
    if (stream) { stream.close(); stream = null; }
    btn.disabled = false;
    btn.textContent = '▶ Replay';
    $('#replay-foot').textContent = text;
  };

  stream.addEventListener('done', (e) => {
    const d = JSON.parse(e.data);
    finish(`${d.steps} steps · ${d.summary || (d.succeeded ? 'goal met' : 'gave up')}`);
  });
  // EventSource retries by default; a completed stream closes server-side
  // and would otherwise reconnect and replay forever.
  stream.onerror = () => finish('stream ended');
}

/* ------------------------------------------------------- intelligence -- */

async function loadIntel() {
  loadAlerts();
  loadForecasts();
  loadInsights();
}

async function loadAlerts() {
  const el = $('#alerts');
  const grain = $('#alert-grain').value;
  el.innerHTML = '<div class="loading">loading…</div>';
  try {
    const d = await api(`/api/v1/anomalies?limit=25${grain ? `&grain=${grain}` : ''}`);
    if (!d.rows.length) { el.innerHTML = '<div class="empty">no anomalies</div>'; return; }
    $('#nav-alert-count').textContent = d.rows.length;
    el.innerHTML = d.rows.map((r) => {
      const z = Math.abs(Number(r.score ?? r.z_score ?? 0));
      const sev = z > 6 ? 'high' : z > 4 ? 'med' : 'low';
      const dev = Number(r.deviation_pct);
      return `<div class="alert">
        <span class="alert-sev ${sev}"></span>
        <div class="alert-body">
          <div class="alert-top">
            <span class="alert-name">${esc(r.entity_name || r.entity_key)}</span>
            <span class="code">${esc(r.entity_key)}</span>
            <span class="pill">${esc(r.period)}</span>
            <span class="pill">${esc(r.method || 'statistical')}</span>
          </div>
          <div class="alert-detail">
            observed ${num(r.observed_mt)} MT · expected ${num(r.expected_mt)} MT
            ${Number.isFinite(dev) ? ` · ${dev >= 0 ? '+' : ''}${dev.toFixed(1)}%` : ''}
          </div>
        </div>
      </div>`;
    }).join('');
  } catch { el.innerHTML = '<div class="empty">could not load alerts</div>'; }
}

async function loadForecasts() {
  const el = $('#forecasts');
  try {
    const d = await api('/api/v1/forecasts?limit=15');
    if (!d.rows.length) { el.innerHTML = '<div class="empty">no forecasts</div>'; return; }
    // Horizon and direction are what distinguish otherwise identical rows:
    // seasonal_naive repeats the last seasonal value at every horizon, so
    // without them three legitimate forecasts read as one duplicated three times.
    el.innerHTML = `<div class="rows">${d.rows.map((r) => `
      <div class="row fc-row">
        <span class="name">
          <strong>${esc(r.entity_name || r.entity_key)}</strong>
          <span class="pill">h+${esc(r.horizon)}</span>
          <span class="pill">${esc(String(r.direction || '').toLowerCase())}</span>
          <div class="fc-band">${esc(r.model || '')} · ${esc(r.period)} ·
            ${num(r.lower_mt, 0)} – ${num(r.upper_mt, 0)} MT</div>
        </span>
        <span class="num">${num(r.predicted_mt)}</span>
        <span class="pill ${Number(r.backtest_mape_pct) <= 12 ? 'ok' : ''}">${
          Number.isFinite(Number(r.backtest_mape_pct)) ? Number(r.backtest_mape_pct).toFixed(1) + '% MAPE' : '—'}</span>
      </div>`).join('')}</div>`;
  } catch { el.innerHTML = '<div class="empty">could not load forecasts</div>'; }
}

async function loadInsights() {
  const el = $('#insights');
  try {
    const d = await api('/api/v1/insights?limit=12');
    if (!d.rows.length) {
      el.innerHTML = '<div class="empty">No written insights yet — run the analytics stage to generate them.</div>';
      return;
    }
    el.innerHTML = d.rows.map((r) => `
      <div class="insight">
        <h3>${esc(r.headline)}</h3>
        <p>${esc(r.narrative)}</p>
      </div>`).join('');
  } catch { el.innerHTML = '<div class="empty">could not load insights</div>'; }
}

/* ---------------------------------------------------------------- ask -- */

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
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    });

    const cols = d.rows.length ? Object.keys(d.rows[0]) : [];
    const table = d.rows.length
      ? `<table class="answer-table">
           <tr>${cols.map((c) => `<th>${esc(c.replace(/_/g, ' '))}</th>`).join('')}</tr>
           ${d.rows.slice(0, 6).map((r) => `<tr>${cols.map((c) => {
             const v = r[c];
             const n = typeof v === 'number' || (!Number.isNaN(Number(v)) && v !== null && v !== '');
             return `<td class="${n ? 'n' : ''}">${n ? num(v) : esc(v)}</td>`;
           }).join('')}</tr>`).join('')}
         </table>`
      : '';

    const passages = (d.passages || []).slice(0, 2).map((p) => `
      <div class="passage">
        <div class="p-src">${esc(p.publisher)}${p.page ? ` · page ${p.page}` : ''} · score ${p.score}</div>
        ${esc(String(p.excerpt).slice(0, 190))}…
      </div>`).join('');

    const cites = (d.citations || []).slice(0, 3).map((c) =>
      `<a href="${esc(c.source_url)}" target="_blank" rel="noopener">↗ ${esc(c.publisher)} — ${esc(c.title || c.source_url)}</a>`).join('');

    thinking.innerHTML = `
      <div class="meta">
        <span class="pill ${d.grounded ? 'ok' : 'bad'}">${d.grounded ? 'grounded' : 'ungrounded'}</span>
        <span class="pill">${esc(d.intent)}</span>
        ${d.retrieval ? `<span class="pill">${esc(d.retrieval.backend.split('(')[0])}</span>` : ''}
      </div>
      <p>${esc(d.answer)}</p>
      ${table}
      ${passages ? `<div style="margin-top:8px"><div class="p-src" style="color:var(--muted);font-size:11px">retrieved passages</div>${passages}</div>` : ''}
      ${cites ? `<div class="cites">${cites}</div>` : ''}
      <p class="step-ms" style="margin-top:7px">read as: ${esc(d.understood_as)}</p>`;
  } catch (e) {
    thinking.innerHTML = `<p>Could not answer that. ${esc(e.message)}</p>`;
  }
}

/* --------------------------------------------------------- provenance -- */

async function loadSources() {
  try {
    const d = await api('/api/v1/sources');
    const el = $('#sources');
    el.innerHTML = `<div class="rows">${d.publishers.map((p) => `
      <div class="row src-row">
        <span class="code">${esc(p.publisher)}</span>
        <span class="name">${int(p.documents)} documents · retrieved ${ago(p.last_retrieved)}</span>
        <span class="num">${int(p.facts)} facts</span>
      </div>`).join('')}</div>`;
  } catch { $('#sources').innerHTML = '<div class="empty">could not load sources</div>'; }

  try {
    const s = await api('/api/v1/search/stats');
    $('#index-stats').innerHTML = `<div class="rows">
      ${s.by_publisher.map((p) => `
        <div class="row src-row">
          <span class="code">${esc(p.publisher)}</span>
          <span class="name">${int(p.documents)} documents indexed</span>
          <span class="num">${int(p.chunks)} passages</span>
        </div>`).join('')}
      <div class="row src-row">
        <span class="code">MODEL</span>
        <span class="name">${esc(s.embed_models.join(', ') || 'none')}</span>
        <span class="num">${int(s.vocabulary_terms)} terms</span>
      </div>
    </div>`;
  } catch { $('#index-stats').innerHTML = '<div class="empty">index unavailable</div>'; }
}

async function runSearch(q) {
  const el = $('#search-results');
  el.innerHTML = '<div class="loading">searching…</div>';
  try {
    const d = await api(`/api/v1/search?q=${encodeURIComponent(q)}&top_k=6`);
    if (!d.passages.length) { el.innerHTML = '<div class="empty">nothing matched</div>'; return; }
    el.innerHTML = d.passages.map((p) => `
      <div class="result">
        <div class="result-head">
          <span class="code">${esc(p.publisher)}</span>
          ${p.page ? `<span class="pill">page ${p.page}</span>` : ''}
          <span class="pill">${p.score}</span>
          <a href="${esc(p.source_url)}" target="_blank" rel="noopener">↗ document</a>
        </div>
        <div class="result-body">${esc(String(p.excerpt).slice(0, 300))}…</div>
      </div>`).join('');
  } catch { el.innerHTML = '<div class="empty">search failed</div>'; }
}

/* -------------------------------------------------------------- boot -- */

const LOADERS = {
  overview: loadOverview,
  agents: loadAgents,
  intel: loadIntel,
  ask: () => {},
  sources: loadSources,
};

async function loadPipeline() {
  try {
    const p = await api('/api/v1/pipeline/state');
    const chip = $('#pipeline-chip');
    chip.classList.add(p.ok ? 'ok' : 'bad');
    $('#pipeline-label').textContent = p.ok ? `pipeline ok · ${ago(p.finished_at)}` : 'last run failed';
    $('#freshness').textContent = `last ingest ${ago(p.finished_at)}`;
  } catch {
    $('#pipeline-label').textContent = 'pipeline unknown';
    $('#freshness').textContent = '';
  }
}

function init() {
  const saved = localStorage.getItem('aci-theme');
  if (saved) document.documentElement.dataset.theme = saved;

  on('#theme-toggle', 'click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('aci-theme', next); } catch { /* private mode */ }
  });

  const rail = $('.rail');
  const scrim = $('#scrim');
  const closeRail = () => {
    rail?.classList.remove('is-open');
    if (scrim) scrim.hidden = true;
  };
  on('#menu-btn', 'click', () => {
    rail?.classList.toggle('is-open');
    if (scrim) scrim.hidden = !rail?.classList.contains('is-open');
  });
  on(scrim, 'click', closeRail);

  $$('.nav-item').forEach((b) =>
    b.addEventListener('click', () => { show(b.dataset.view); closeRail(); }));
  on('#rank-direction', 'change', loadRankings);
  on('#rank-order', 'change', loadRankings);
  on('#alert-grain', 'change', loadAlerts);
  on('#run-agent', 'change', loadRuns);
  on('#replay-btn', 'click', replay);
  on('#replay-run', 'change', (e) => showRun(e.target.value));

  if ($('#suggestions')) $('#suggestions').innerHTML = SUGGESTIONS.map((s) => `<button class="chip">${esc(s)}</button>`).join('');
  $$('#suggestions .chip').forEach((c) => c.addEventListener('click', () => ask(c.textContent)));

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

  loadPipeline();
  show(location.hash.slice(1) in VIEWS ? location.hash.slice(1) : 'overview');
}

document.addEventListener('DOMContentLoaded', () => {
  try {
    init();
  } catch (e) {
    // Last resort: get the default view on screen even if wiring failed,
    // so a broken control degrades to a static page rather than a blank one.
    console.error('init failed', e);
    try { show('overview'); } catch { /* nothing left to do */ }
  }
});
