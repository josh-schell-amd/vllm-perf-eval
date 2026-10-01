// The page: state, rendering and charts. Pure logic is in analysis.js.
'use strict';
(function() {

const {
  esc,
  safeUrl,
  integer,
  fmt,
  shortModel,
  shortSha,
  pct,
  pctDirection,
  median,
  parseTime,
  fmtDate,
  nightlyDay,
  dayX,
  verdict,
  accuracyVerdict,
  nightKey,
  isOngoing,
  reportedTonight,
  comparedOvernight,
  REG_TIERS,
  regTier,
  configKeyOf,
  isRetired,
  lenLabel,
  shapeLabel,
  variantLabel,
  compact,
  accuracyMetricLabel,
  accuracyPercent,
  accuracyPoints,
  rankNightlies,
  chartBoundsFor,
  runDay,
  runRows,
  sortRows,
  toCsv,
  nightlyStatus,
} = window.PerfAnalysis;

/* ================================================================
   1. COLOUR SEMANTICS
   ================================================================ */
// PALETTE has no red or pink, so a red line always means "regressed", and no
// grey, which reads as disabled.
const PALETTE = ['#6aa6ed', '#6dbf80', '#c49af5', '#4fc3c3', '#ddb040', '#c8a27a', '#9ad06a',
  '#7c83f0', '#5fc9a0', '#c8b06a', '#79c0ff', '#d0a3e8', '#4fa3a3', '#a3c46a'
];
// The same hues, index for index, dark enough to read on white: a series keeps
// its identity across themes. Add to both lists together.
const PALETTE_LIGHT = ['#2563b8', '#2e8b47', '#7b4cc0', '#138a8a', '#8f6a00', '#8b5a2b', '#5a8a1f',
  '#4a50c8', '#1f8a64', '#85702a', '#1f78c8', '#9050b8', '#2c7676', '#66802a'
];
function isLightTheme() { return document.documentElement.getAttribute('data-theme') === 'light'; }
function activePalette() { return isLightTheme() ? PALETTE_LIGHT : PALETTE; }
// Regression red, read from --accent-red at draw time; this is the fallback.
// A series turns red only on the metrics it regressed on.
const REG_COLOR = '#f25c4e';

// Days of history the page shows, ending now, so a stalled nightly shows as
// empty rather than as old data. Set from the payload (aggregate.py owns it);
// this is the fallback. The charts can narrow it (chartDays) but not widen it.
let WINDOW_DAYS = 30;
// The headline metric for the Performance overnight card.
const PRIMARY_METRIC = 'output_tput_per_gpu';
const FACETS = [
  { key: 'device', label: 'Device', get: c => c.device },
  { key: 'model', label: 'Model', get: c => c.shortModel },
  // A recipe that names no precision reads "unstated", never a guess.
  { key: 'precision', label: 'Precision', get: c => c.precision || 'unstated' },
  { key: 'shape', label: 'ISL/OSL', get: c => c.isl + '/' + c.osl },
  { key: 'conc', label: 'Concurrency', get: c => String(c.conc) },
];

/* ================================================================
   2. HELPERS
   ================================================================ */


// A run with failed requests is not comparable to a clean one; say so wherever it shows.
function hadFailures(p) { return !!p && p.failed_requests > 0; }

// The key for the orange ▲ that marks a run with failed requests on a chart.
function failedKey() {
  return '<span class="hint-key"><span class="failed-mark">▲</span>run had failed requests: '
    + 'not comparable to a clean run</span>';
}
function failedNote(p) {
  if (!(p && p.failed_requests > 0)) return '';
  const failed = integer(Math.round(p.failed_requests));
  // Without a completed count there is no total to state.
  if (p.completed_requests == null) return failed + ' requests failed';
  return failed + ' of ' + integer(Math.round(p.failed_requests + p.completed_requests)) + ' requests failed';
}





function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

// Makes a non-button element keyboard-operable. No role="button": these
// are often table cells, and the role would break table semantics.
function activatable(el, label, handler) {
  el.setAttribute('tabindex', '0');
  if (label) el.setAttribute('aria-label', label);
  el.addEventListener('click', handler);
  el.addEventListener('keydown', event => {
    // Keys on a nested link or control belong to it, so Enter follows the link.
    if (event.target !== el && event.target.closest
      && event.target.closest('a,button,input,select,textarea')) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      handler(event);
    }
  });
}
// Renders replace the controls, so restore focus to the matching new one.
function focusAfterRender(selector) {
  const el = document.querySelector(selector);
  if (el) el.focus();
}

function cssEscape(value) {
  return window.CSS && CSS.escape ? CSS.escape(String(value)) : String(value).replace(/["\\]/g, '\\$&');
}





// Drawn: the ↗ glyph renders inconsistently. aria-hidden because the
// link text already says where it goes.
const EXT_ARROW = '<svg class="ext-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
  + 'stroke-width="2.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M6 18 18 6"/><path d="M8.5 6H18v9.5"/></svg>';
const EXT_INLINE = '<span class="ext-inline">' + EXT_ARROW + '</span>';

function buildLink(p, label, cls) {
  const text = esc(label == null ? ('#' + (p.build_number || '?')) : label);
  if (!p.build_url) return text;
  // Every outbound link carries the arrow, so leaving the page is no surprise.
  return '<a href="' + esc(p.build_url) + '" target="_blank" rel="noopener noreferrer"'
    + (cls ? ' class="' + cls + '"' : '') + '>' + text + EXT_INLINE + '</a>';
}

/* ================================================================
   3. DATA
   ================================================================ */
let DATA = null,
  METRICS = [],
  METRIC_BY_KEY = {},
  CONFIGS = [],
  ACC_TASKS = [],
  // Smallest moves that count: { perf_rel, accuracy_abs }, from the payload.
  THRESHOLDS = null;
let BASE_COLOR = {},
  historyChart = null;

// Stored in canonical units (latency in seconds); display_scale converts
// per metric for the axis.
function display(value, metric) {
  if (value == null || !isFinite(value)) return null;
  return metric.display_scale ? value * metric.display_scale : value;
}

function unitOf(metric) { return metric.display_unit || metric.unit || ''; }

// "Total Throughput (tok/s/GPU)". Read from the registry, so renaming a metric
// cannot leave a stale unit behind on an axis.
function axisTitle(metric, fallback) {
  if (!metric) return fallback || '';
  const unit = unitOf(metric);
  return metric.label + (unit ? ' (' + unit + ')' : '');
}

// A series point names its build; the payload lists each build's date and
// provenance once, in `builds`.
function withBuild(p, jobs) {
  const b = DATA.builds[p.build] || {};
  const root = safeUrl(b.build_url);
  const job = (jobs || {})[p.build];
  return {
    t: parseTime(b.date),
    value: Number(p.value),
    date: b.date,
    day: nightlyDay(b),
    vllm_commit: b.vllm_commit,
    image: b.image,
    night: nightKey(b, p.build),
    // The job that ran this config, where known; the whole build otherwise.
    build_url: root && /^[0-9a-f-]{36}$/i.test(job || '') ? root + '#' + job : root,
    build_root_url: root,
    build_number: p.build,
  };
}

// Flatten model -> config -> metric -> series into (config, metric) series.
function buildConfigs(payload) {
  const out = [];
  (payload.models || []).forEach(model => {
    (model.perf_configs || []).forEach(cfg => {
      const metrics = {},
        nights = new Map();
      Object.keys(cfg.metrics || {}).forEach(mk => {
        const pts = ((cfg.metrics[mk] || {}).series || [])
          .map(p => ({
            ...withBuild(p, cfg.jobs),
            completed_requests: p.completed_requests,
            failed_requests: p.failed_requests
          }))
          .filter(p => p.t > 0 && Number.isFinite(p.value))
          .sort((a, b) => a.t - b.t);
        if (!pts.length) return;
        metrics[mk] = pts;
        pts.forEach(p => nights.set(p.night, dayX(p.day)));
      });
      if (!Object.keys(metrics).length) return;
      const sm = shortModel(model.model);
      out.push({
        key: configKeyOf({ ...cfg, model: model.model }),
        model: model.model,
        shortModel: sm,
        device: cfg.device || '',
        precision: cfg.precision || '',
        parallel_label: cfg.parallel_label || '—',
        gpus: cfg.gpus,
        isl: cfg.isl,
        osl: cfg.osl,
        conc: cfg.conc,
        // Never truncated: the concurrency suffix names the config.
        label: sm + ' · ' + (cfg.device || '?') + ' · ' + cfg.isl + '/' + cfg.osl + ' · c=' + cfg.conc,
        metrics: metrics,
        nights: nights,
      });
    });
  });
  // A recipe change (parallelism, precision) starts a new series; name both apart.
  const seen = new Map();
  out.forEach(c => seen.set(c.label, (seen.get(c.label) || 0) + 1));
  out.forEach(c => {
    if (seen.get(c.label) > 1) c.label += ' · ' + variantLabel(c);
  });
  return out.sort(compareConfigs);
}

// Model, device, ISL/OSL, concurrency; numbers compare as numbers, so c=64
// sorts before c=128.
function compareConfigs(a, b) {
  const num = (x, y) => (Number(x) || 0) - (Number(y) || 0);
  return a.shortModel.localeCompare(b.shortModel)
    || String(a.device).localeCompare(String(b.device))
    || num(a.isl, b.isl) || num(a.osl, b.osl) || num(a.conc, b.conc)
    || String(a.precision).localeCompare(String(b.precision))
    || num(a.gpus, b.gpus) || String(a.parallel_label).localeCompare(String(b.parallel_label));
}

function buildAccuracy(payload) {
  const out = [];
  (payload.models || []).forEach(model => {
    (model.accuracy_tasks || []).forEach(task => {
      const pts = (task.series || [])
        .map(p => withBuild(p, task.jobs))
        .filter(p => p.t > 0 && Number.isFinite(p.value))
        .sort((a, b) => a.t - b.t);
      if (!pts.length) return;
      out.push({
        key: [model.model, task.workload || '', task.device || '', task.task, task.metric].join('|'),
        model: model.model,
        shortModel: shortModel(model.model),
        device: task.device || '',
        workload: task.workload || '',
        task: task.task,
        metric: task.metric,
        primary: !!task.primary,
        points: pts,
      });
    });
  });
  return out;
}

// One row per model, device and task: the headline score plus the others.
function accuracyGroups(series) {
  const groups = new Map();
  series.forEach(s => {
    const k = [s.model, s.workload, s.device, s.task].join('|');
    if (!groups.has(k)) groups.set(k, {
      model: s.model,
      shortModel: s.shortModel,
      device: s.device,
      workload: s.workload,
      task: s.task,
      primary: null,
      others: []
    });
    const g = groups.get(k);
    if (s.primary && !g.primary) g.primary = s;
    else g.others.push(s);
  });
  return [...groups.values()].map(g => {
    if (!g.primary) g.primary = g.others.shift();
    g.others.sort((a, b) => a.metric.localeCompare(b.metric));
    return g;
  }).sort((a, b) => a.shortModel.localeCompare(b.shortModel)
    || a.device.localeCompare(b.device) || a.task.localeCompare(b.task));
}


/* ================================================================
   4. STATE
   ================================================================ */
const state = {
  tab: 'performance',
  perfMetric: null, // Performance tab bar metric; null = the default
  selected: {}, // facet key -> Set of allowed values (empty = all)
  onlyRegressed: false, // charts and table show only regressed configs
  onlyFailed: false, // only configs whose newest run had failed requests
  chartDays: null, // trend chart window in days; null = the full window
  runsSort: null, // Data tab column key, '-' prefixed for descending; null = newest first
};
FACETS.forEach(f => state.selected[f.key] = new Set());

function windowCutoff() { return Date.now() - WINDOW_DAYS * 86400000; }

// Chart window, 1 day up to WINDOW_DAYS. Charts only: the comparisons need
// the previous run, which a short window would drop.
function chartDays() {
  const days = Math.round(state.chartDays || WINDOW_DAYS);
  return Math.min(Math.max(days, 1), WINDOW_DAYS);
}
// Shared x-axis bounds, so the stacked charts line up by date (chartBoundsFor).
// Cached per render; renderAll clears it.
let _chartBounds = null;

function chartBounds() {
  if (_chartBounds) return _chartBounds;
  // Newest nightly overall, ignoring filters, so every chart shares the anchor.
  const night = latestNightly();
  let anchor = 0;
  CONFIGS.forEach(c => { const x = c.nights.get(night); if (x > anchor) anchor = x; });
  return (_chartBounds = chartBoundsFor(Date.now(), WINDOW_DAYS, chartDays(), anchor));
}

function chartPointsIn(cfg, metricKey) {
  const min = chartBounds().min;
  return pointsIn(cfg, metricKey).filter(p => dayX(p.day) >= min);
}
// Points inside the window, for one config and metric.
function pointsIn(cfg, metricKey) {
  const cutoff = windowCutoff();
  return (cfg.metrics[metricKey] || []).filter(p => p.t >= cutoff);
}

function facetMatches(cfg, exceptKey) {
  return FACETS.every(f => {
    if (f.key === exceptKey) return true;
    const sel = state.selected[f.key];
    return sel.size === 0 || sel.has(f.get(cfg));
  });
}

function hasPointsInWindow(cfg) {
  return Object.keys(cfg.metrics).some(mk => pointsIn(cfg, mk).length > 0);
}

function visibleConfigs() {
  return CONFIGS.filter(c => facetMatches(c) && hasPointsInWindow(c));
}

// What the charts and table draw. Not visibleConfigs, which the regression
// scan itself runs over.
function shownConfigs() {
  let configs = visibleConfigs();
  if (state.onlyRegressed) {
    const regressed = regressedKeys();
    configs = configs.filter(c => regressed.has(c.key));
  }
  if (state.onlyFailed) configs = configs.filter(hadFailedRequests);
  return configs;
}

// Whether a config's newest run in the window had failed requests, as
// vllm bench serve reports them.
function hadFailedRequests(c) {
  const newest = Object.values(c.metrics).map(pts => pts.filter(p => p.t >= windowCutoff()).at(-1))
    .filter(Boolean).sort((a, b) => b.t - a.t)[0];
  return !!newest && newest.failed_requests > 0;
}

/* ================================================================
   5. ANALYSIS
   ================================================================ */


// Every (config, metric) verdict, cached per filter state.
const _scanCache = new Map();

function scanKey() {
  return FACETS.map(f => [...state.selected[f.key]].sort().join(',')).join(';');
}
// Nightlies across all configs, ignoring filters, newest first: every overnight
// figure describes the first, compared against the second. CONFIGS is fixed
// once the payload loads, so this is worked out once.
let _nightlies = null;
function recentNightlies() {
  if (_nightlies) return _nightlies;
  const nights = new Map();
  const add = p => {
    if (!nights.has(p.night)) nights.set(p.night, { x: dayX(p.day), builds: [] });
    const n = nights.get(p.night);
    if (!n.builds.includes(String(p.build_number))) n.builds.push(String(p.build_number));
  };
  CONFIGS.forEach(c => Object.values(c.metrics).forEach(pts => pts.forEach(add)));
  const ranked = rankNightlies(nights);
  return (_nightlies = { ranked: ranked, nights: nights });
}
function latestNightly() { return recentNightlies().ranked[0] || null; }
function previousNightly() { return recentNightlies().ranked[1] || null; }
// How labels name a nightly: its build, or builds when it was rebuilt ("605/607").
function nightLabel(night) {
  const n = recentNightlies().nights.get(night);
  return n ? [...n.builds].sort((a, b) => a - b).join('/') : null;
}
function latestNightlyBuild() { return nightLabel(latestNightly()); }
function previousNightlyBuild() { return nightLabel(previousNightly()); }

function scan() {
  const ck = scanKey();
  if (_scanCache.has(ck)) return _scanCache.get(ck);
  const out = scanConfigs(visibleConfigs());
  _scanCache.set(ck, out);
  return out;
}

function scanConfigs(configs) {
  const latest = latestNightly(),
    previous = previousNightly();
  const out = [];
  configs.forEach(cfg => {
    METRICS.forEach(metric => {
      const pts = pointsIn(cfg, metric.key);
      if (!comparedOvernight(pts, latest, previous)) return;
      const v = verdict(pts.map(p => p.value), metric, THRESHOLDS.perf_rel);
      if (!v) return;
      out.push({
        configKey: cfg.key,
        config: cfg,
        metricKey: metric.key,
        metric: metric,
        row: pts[pts.length - 1],
        ...v
      });
    });
  });
  out.sort((a, b) => b.magnitude - a.magnitude);
  return out;
}

function invalidateScan() { _scanCache.clear(); }

// Configs regressed on any metric, ignoring the filters. scan() only covers
// what the filters show, but a facet count asks about configs they hide: with
// Model A picked, Model B's count must still know whether B regressed. Filters
// cannot change a config's own verdict, so this needs no invalidation.
let _regressedAny = null;
function regressedUnfiltered() {
  if (!_regressedAny) {
    _regressedAny = configsIn(scanConfigs(CONFIGS.filter(hasPointsInWindow))
      .filter(d => d.regressed && d.metric.counted));
  }
  return _regressedAny;
}

// Derived metrics (input throughput, interactivity) repeat another metric's
// move, so counts leave them out; charts still ring them.
const countedOf = () => scan().filter(d => d.metric.counted);
const regressionsOf = () => countedOf().filter(d => d.regressed);
const improvementsOf = () => countedOf().filter(d => d.improved && d.magnitude >= THRESHOLDS.perf_rel);
// Moves under the threshold, shown beside the counts they were left out of.
const belowThresholdOf = () => countedOf().filter(d => d.ratio !== 0 && !d.improved && !d.regressed);
const belowThresholdGainsOf = () => countedOf().filter(d => d.improved && d.magnitude < THRESHOLDS.perf_rel);

function thresholdLabel() { return +(THRESHOLDS.perf_rel * 100).toFixed(2) + '%'; }

// Counts are of configs: one slowdown moves several metrics at once.
const configsIn = items => new Set(items.map(d => d.configKey));
function regressedKeys() { return configsIn(regressionsOf()); }
// Configs with only moves under the threshold, in that direction.
function belowOnly(below, counted) {
  const out = configsIn(below);
  configsIn(counted).forEach(k => out.delete(k));
  return out;
}
// Regressed on *this* metric, not merely somewhere.
function regressedKeysForMetric(metricKey) {
  return configsIn(scan().filter(d => d.regressed && d.metricKey === metricKey));
}

// Median night-over-night change across configs; one config's delta is noise.
function nightOverNight() {
  const metric = METRIC_BY_KEY[PRIMARY_METRIC];
  if (!metric) return null;
  const latest = latestNightly(),
    previous = previousNightly();
  const ratios = [];
  visibleConfigs().forEach(cfg => {
    const pts = pointsIn(cfg, PRIMARY_METRIC);
    if (!comparedOvernight(pts, latest, previous)) return;
    const prev = pts[pts.length - 2].value;
    if (!prev) return;
    ratios.push((pts[pts.length - 1].value - prev) / Math.abs(prev));
  });
  if (!ratios.length) return null;
  const med = median(ratios);
  return {
    ratio: med,
    configs: ratios.length,
    improved: metric.better === 'higher' ? med > 0 : med < 0
  };
}


function coverage() {
  // Filtered like the other cards.
  const expected = DATA.expected.configs.filter(e => facetMatches({
    device: e.device,
    shortModel: shortModel(e.model),
    precision: e.precision,
    isl: e.isl,
    osl: e.osl,
    conc: e.conc,
  }));
  if (!expected.length) return null;

  const latest = latestNightly();
  if (latest === null) return null;

  // Which expected configs reported in that nightly.
  const reported = new Set();
  CONFIGS.forEach(c => {
    if (!c.nights.has(latest)) return;
    reported.add(configKeyOf(c));
  });

  const missing = expected.filter(e => !reported.has(configKeyOf(e)));

  // Grouped by workload: one failed step drops every config in it, which is
  // one incident, not one per shape.
  const groupKey = e => e.workload || ((e.model || '') + '|' + (e.device || ''));
  const byWorkload = new Map();
  expected.forEach(e => {
    const key = groupKey(e);
    if (!byWorkload.has(key)) {
      byWorkload.set(key, {
        name: (e.model ? shortModel(e.model) : e.workload) + ' · ' + (e.device || '?'),
        total: 0,
        shapes: [],
      });
    }
    byWorkload.get(key).total++;
  });
  missing.forEach(e => {
    byWorkload.get(groupKey(e)).shapes.push(e.isl + '/' + e.osl + ' c=' + e.conc);
  });

  const groups = [...byWorkload.values()]
    .filter(g => g.shapes.length)
    .map(g => ({ ...g, whole: g.shapes.length === g.total }))
    // Whole-workload outages first, then by size of the gap.
    .sort((a, b) => (b.whole - a.whole) || (b.shapes.length - a.shapes.length)
      || a.name.localeCompare(b.name));

  return {
    present: expected.length - missing.length,
    expected: expected.length,
    build: latestNightlyBuild(),
    missing: missing.length,
    groups,
    accuracy: accuracyTonight(),
  };
}

// After the newest nightly with AMD results: newer nightlies with none, and
// days with no nightly (nightlyStatus). Judged at collection time, so a
// stopped collector is reported as that, not as missing nightlies.
function nightlyGaps() {
  const latest = latestRun();
  return nightlyStatus(DATA.nightly_runs,
    latest ? { build: String(latest.build_number), day: latest.day } : null,
    parseTime(DATA.generated_at));
}

// A point from the latest nightly, for the card that names it. Unfiltered,
// and the same nightly every other card compares.
function latestRun() {
  const night = latestNightly();
  let found = null;
  CONFIGS.forEach(c => Object.keys(c.metrics).forEach(mk => {
    pointsIn(c, mk).forEach(p => { if (!found && p.night === night) found = p; });
  }));
  return found;
}
// Distinct nightlies in the window, ignoring filters.
function nightliesInWindow() {
  const nights = new Set();
  CONFIGS.forEach(c => Object.keys(c.metrics).forEach(mk => {
    pointsIn(c, mk).forEach(p => nights.add(p.night));
  }));
  return nights.size;
}

/* ================================================================
   6. CONTROLS
   ================================================================ */
// How a nightly with no AMD results ended, from its Buildkite state.
function runOutcome(run) {
  if (run.state === 'canceled' || run.state === 'canceling') return 'was canceled';
  // Every nightly's build state is usually failed (some job always is), so
  // the state is noted, not taken as the reason.
  return 'ran, but no AMD workload produced results'
    + (run.state && run.state !== 'passed' ? ' (build ' + esc(run.state) + ')' : '');
}

// One gap as text: that day's nightlies and how each ended, or that none ran.
function gapText(gap, known, links) {
  if (gap.runs.length) {
    return gap.runs.map(run => (links
      ? buildLink({ build_number: run.build, build_url: safeUrl(run.build_url) }, null)
      : '#' + esc(run.build)) + ' ' + runOutcome(run)).join('; ');
  }
  return known ? 'no nightly ran' : 'no AMD results';
}

// A nightly still going, and what AMD work it has left. Not a problem:
// its results are collected once its AMD jobs finish.
function runningText(run) {
  const left = run.amd_pending || [];
  return '#' + esc(run.build) + ' still running'
    + (left.length ? ', waiting on ' + left.map(esc).join(', ')
      : ': AMD jobs done, collected on the next run');
}

// Collection runs hourly by day; older than this, it has missed a day.
const COLLECTOR_STALE_MS = 36 * 3600000;

// Unfiltered: an empty filter combination is not a stalled nightly.
function renderNotice() {
  const host = document.getElementById('notice-host');
  const lines = [];
  let heading = '';
  const latest = latestRun();
  if (!latest) {
    heading = 'No AMD nightly results in the last ' + WINDOW_DAYS + ' days.';
  } else {
    const status = nightlyGaps();
    if (status.gaps.length) {
      heading = 'No AMD results since the ' + esc(latest.day) + ' nightly ('
        + buildLink({ build_number: latest.build_number, build_url: latest.build_root_url }, null) + ').';
      status.gaps.forEach(gap => lines.push(esc(gap.day) + ' · ' + gapText(gap, status.known, true)));
    }
  }
  const collected = parseTime(DATA.generated_at);
  if (collected && Date.now() - collected > COLLECTOR_STALE_MS) {
    lines.push('Results were last collected ' + esc(relTime(collected))
      + ': the Collect and Deploy workflow may not be running.');
    heading = heading || 'This page is out of date.';
  }
  if (!heading) { host.innerHTML = ''; return; }
  host.innerHTML = '<div class="notice"><div><strong>' + heading + '</strong>'
    + (lines.length ? '<ul>' + lines.map(line => '<li>' + line + '</li>').join('') + '</ul>' : '')
    + 'Check the <a href="' + esc(safeUrl(DATA.pipeline.url)) + '" target="_blank" rel="noopener noreferrer">'
    + 'perf-eval pipeline</a>.</div></div>';
}

// Selected values stay listed with no matching config (a stale shared link,
// say), or they could never be unticked.
function facetValues(f) {
  const values = [...new Set([...CONFIGS.map(f.get), ...state.selected[f.key]])]
    .filter(v => v !== undefined && v !== '');
  return values.sort(f.key === 'conc' ? (a, b) => (+a) - (+b) : (a, b) => String(a).localeCompare(String(b)));
}
// The configs a value adds under the other active filters, so it can read 0.
// Only regressed is one of those filters: without it, a value with no
// regressions would stay clickable and select nothing.
function facetCount(f, v) {
  const regressed = state.onlyRegressed ? regressedUnfiltered() : null;
  return CONFIGS.filter(c => f.get(c) === v && facetMatches(c, f.key) && hasPointsInWindow(c)
    && (!regressed || regressed.has(c.key)) && (!state.onlyFailed || hadFailedRequests(c))).length;
}
function activeFilterCount() {
  return FACETS.reduce((n, f) => n + state.selected[f.key].size, 0) + (state.onlyRegressed ? 1 : 0)
    + (state.onlyFailed ? 1 : 0);
}

function facetOptions(f) {
  return facetValues(f).map(v => {
    const on = state.selected[f.key].has(v);
    const count = facetCount(f, v);
    return { value: v, on: on, count: count, unavailable: count === 0 && !on };
  });
}

function applyFilters(focusSelector) {
  invalidateScan();
  syncToHash();
  renderAll();
  if (focusSelector) focusAfterRender(focusSelector);
}

function setFacet(key, update) {
  const sel = state.selected[key];
  if (update.clear) sel.clear();
  else if (update.on) sel.add(update.value);
  else sel.delete(update.value);
}

function resetFilters() {
  FACETS.forEach(f => state.selected[f.key].clear());
  state.onlyRegressed = false;
  state.onlyFailed = false;
  applyFilters();
}

// ATOM-style dropdowns in the header, on the same state as the filter panel.
let openFilter = null;

function renderHeaderFilters() {
  const bar = document.getElementById('filter-bar');
  const active = activeFilterCount();
  bar.innerHTML = FACETS.map(f => {
    const sel = state.selected[f.key];
    const open = openFilter === f.key;
    return '<div class="filter-group"><button class="filter-btn' + (open ? ' open' : '') + '" data-filter="' + esc(f.key) + '"'
      + ' aria-expanded="' + (open ? 'true' : 'false') + '">' + esc(f.label) + ' ▾'
      + (sel.size ? '<span class="count">' + sel.size + '</span>' : '') + '</button>'
      + '<div class="filter-dropdown' + (open ? ' open' : '') + '" data-dropdown="' + esc(f.key) + '">'
      + '<label><input type="checkbox" data-all' + (sel.size ? '' : ' checked') + '> All</label><div class="sep"></div>'
      + facetOptions(f).map(opt =>
        '<label' + (opt.unavailable ? ' class="unavailable"' : '') + '><input type="checkbox" value="' + esc(opt.value) + '"'
        + (opt.on ? ' checked' : '') + (opt.unavailable ? ' disabled' : '') + '> <span class="opt-label">' + esc(opt.value)
        + '</span><span class="opt-count">' + opt.count + '</span></label>').join('')
      + '</div></div>';
  }).join('')
    + toggleButton('only-regressed', state.onlyRegressed, 'Regressed', regressedKeys().size,
      'Show only configurations that regressed overnight')
    + toggleButton('only-failed', state.onlyFailed, 'Failed requests', visibleConfigs().filter(hadFailedRequests).length,
      'Show only configurations whose newest run had failed requests, as vllm bench serve reports them')
    + '<span class="filter-summary">' + (active ? integer(shownConfigs().length) + ' configs' : 'Showing all') + '</span>'
    + '<button class="filter-reset" id="header-reset"' + (active ? '' : ' disabled') + '>Reset</button>';

  bar.querySelectorAll('.filter-btn').forEach(btn => btn.addEventListener('click', event => {
    event.stopPropagation();
    const key = btn.getAttribute('data-filter');
    openFilter = openFilter === key ? null : key;
    renderHeaderFilters();
    focusAfterRender('.filter-btn[data-filter="' + cssEscape(key) + '"]');
  }));
  bar.querySelectorAll('.filter-dropdown').forEach(dd => {
    dd.addEventListener('click', event => event.stopPropagation());
    dd.addEventListener('change', event => {
      const key = dd.getAttribute('data-dropdown');
      const input = event.target;
      if (input.hasAttribute('data-all')) setFacet(key, { clear: true });
      else setFacet(key, { on: input.checked, value: input.value });
      applyFilters('[data-dropdown="' + cssEscape(key) + '"] input'
        + (input.hasAttribute('data-all') ? '[data-all]' : '[value="' + cssEscape(input.value) + '"]'));
    });
  });
  const toggles = { 'only-regressed': 'onlyRegressed', 'only-failed': 'onlyFailed' };
  Object.entries(toggles).forEach(([id, key]) => {
    const btn = document.getElementById(id);
    if (btn) btn.addEventListener('click', () => {
      state[key] = !state[key];
      applyFilters('#' + id);
    });
  });
  const reset = document.getElementById('header-reset');
  if (reset) reset.addEventListener('click', resetFilters);
}

// A filter that is on or off, with how many configs it would keep. Disabled
// at zero unless on, so it can always be turned off.
function toggleButton(id, on, label, count, title) {
  return '<button class="filter-btn toggle' + (on ? ' on' : '') + '" id="' + id + '"'
    + ' aria-pressed="' + (on ? 'true' : 'false') + '" title="' + esc(title) + '"'
    + (count || on ? '' : ' disabled') + '>' + esc(label) + ' <span class="count">' + integer(count) + '</span></button>';
}

function closeHeaderFilters() {
  if (openFilter === null) return;
  openFilter = null;
  renderHeaderFilters();
}

/* ================================================================
   7. KPI ROW
   ================================================================ */
function renderKpis() {
  const host = document.getElementById('kpi-host');
  const configs = visibleConfigs();
  if (!configs.length) { host.innerHTML = ''; return; }

  const regs = configsIn(regressionsOf());
  const imps = configsIn(improvementsOf());
  const non = nightOverNight();
  const cov = coverage();
  const latest = latestRun();
  const cards = [];

  if (latest) {
    // Each later day with no AMD results, and why, turns the card orange.
    const status = nightlyGaps();
    const shown = status.gaps.slice(0, 3);
    cards.push({
      cls: status.gaps.length ? 'warn' : 'linked',
      href: latest.build_root_url,
      label: 'Latest nightly with AMD results',
      value: latest.day,
      sub: 'build #' + esc(latest.build_number || '?') + ' · vLLM <code>' + esc(shortSha(latest.vllm_commit)) + '</code>'
        + shown.map(gap => '<br><span class="warnc">' + esc(gap.day.slice(5)) + ': '
          + gapText(gap, status.known, false).replace('ran, but no AMD workload produced results', 'ran, no AMD results')
            .replace(/ \(build [^)]*\)/, '') + '</span>').join('')
        + (status.gaps.length > shown.length
          ? '<br><span class="warnc">and ' + (status.gaps.length - shown.length) + ' more</span>' : '')
        + status.running.map(run => '<br><span class="neutral">' + esc(runDay(run).slice(5)) + ': '
          + runningText(run) + '</span>').join(''),
    });
  }

  if (non) {
    // Outline shows direction (green up, red down), neutral inside the threshold.
    // Uses `improved`, not the sign, in case the metric becomes lower-is-better.
    // Green for any gain; red only for a drop past the threshold.
    const dropped = !non.improved && non.ratio !== 0 && Math.abs(non.ratio) >= THRESHOLDS.perf_rel;
    cards.push({
      cls: non.improved ? 'ok' : (dropped ? 'alert' : ''),
      label: 'Performance overnight',
      value: pct(non.ratio, 2),
      valueCls: non.improved ? 'up' : (dropped ? 'down' : ''),
      sub: 'median change in ' + esc((METRIC_BY_KEY[PRIMARY_METRIC] || {}).label || PRIMARY_METRIC),
    });
  }

  // Colour only when there is something to report.
  const threshold = thresholdLabel();
  const below = belowOnly(belowThresholdOf(), regressionsOf()).size;
  const belowGains = belowOnly(belowThresholdGainsOf(), improvementsOf()).size;
  // Zero is good news only when something was compared.
  const compared = scan().length > 0;
  const configsWord = n => 'config' + (n === 1 ? '' : 's');
  cards.push({
    cls: regs.size ? 'alert' : (compared ? 'ok' : ''),
    label: 'Regressions overnight',
    value: integer(regs.size),
    valueCls: regs.size ? 'down' : (compared ? 'up' : ''),
    sub: configsWord(regs.size) + ' with a drop of at least <b>' + threshold + '</b>'
      + (below ? '<br><b>' + integer(below) + '</b> more with only smaller drops' : ''),
  });

  cards.push({
    cls: imps.size ? 'ok' : '',
    label: 'Improvements overnight',
    value: integer(imps.size),
    valueCls: imps.size ? 'up' : '',
    sub: configsWord(imps.size) + ' with a gain of at least <b>' + threshold + '</b>'
      + (belowGains ? '<br><b>' + integer(belowGains) + '</b> more with only smaller gains' : ''),
  });

  const acc = accuracyTonight();
  if (acc) {
    cards.push({
      cls: acc.regressed ? 'alert' : (acc.reported ? 'ok' : ''),
      tab: 'accuracy',
      label: 'Accuracy overnight',
      value: integer(acc.regressed),
      valueCls: acc.regressed ? 'down' : (acc.reported ? 'up' : ''),
      sub: 'drops of at least <b>' + acc.threshold + '</b>',
    });
  }

  if (cov) {
    // Perf and accuracy coverage in one card. Each line is coloured on its own;
    // the border is red if either is incomplete.
    const accMissing = cov.accuracy ? cov.accuracy.total - cov.accuracy.reported : 0;
    // Grid cells, so the two lines align on the slash.
    const covRow = (present, total, label) => {
      const cls = present < total ? 'down' : 'up';
      return '<span class="' + cls + ' num">' + integer(present) + '</span>'
        + '<span class="' + cls + '">/</span>'
        + '<span class="' + cls + '">' + integer(total) + '</span>'
        + '<span class="kpi-unit">' + label + '</span>';
    };
    cards.push({
      cls: (cov.missing || accMissing) ? 'alert' : 'ok',
      label: 'Coverage',
      valueCls: 'stacked',
      value: covRow(cov.present, cov.expected, 'performance')
        + (cov.accuracy ? covRow(cov.accuracy.reported, cov.accuracy.total, 'accuracy') : ''),
    });
  }

  host.innerHTML = cards.map(c => {
    const inner = '<div class="kpi-label">' + esc(c.label) + '</div>'
      + '<div class="kpi-value' + (c.valueCls ? ' ' + c.valueCls : '') + '">' + c.value + '</div>'
      + '<div class="kpi-sub">' + (c.sub || '') + '</div>';
    // The whole card is the link, with the arrow in the corner saying so.
    if (c.href) {
      return '<a class="kpi-card linked' + (c.cls && c.cls !== 'linked' ? ' ' + c.cls : '') + '" href="' + esc(c.href)
        + '" target="_blank" rel="noopener noreferrer">'
        + '<span class="kpi-ext">' + EXT_ARROW + '</span>' + inner + '</a>';
    }
    // No arrow on a card that opens a tab: it stays on the page.
    return '<div class="kpi-card' + (c.tab ? ' linked' : '') + (c.cls ? ' ' + c.cls : '') + '"'
      + (c.tab ? ' data-goto-tab="' + esc(c.tab) + '"' : '') + '>' + inner + '</div>';
  }).join('');
  host.querySelectorAll('[data-goto-tab]').forEach(el => {
    const tab = el.getAttribute('data-goto-tab');
    activatable(el, 'Open the ' + tab + ' tab', () => { state.tab = tab;
      syncToHash();
      renderAll(); });
  });
}

// The accuracy results the recipes' `lm_eval.tasks` say should run, each
// paired with the series that reported for it, or null. From the recipes, not
// the window, so a workload whose lm-eval keeps failing stays missing.
function expectedAccuracy(groups) {
  const declared = DATA.expected.accuracy;
  // Keyed on the workload, which names the recipe.
  const key = (workload, device, task) => [workload || '', device || '', task].join('|');
  const byKey = new Map();
  groups.forEach(g => {
    const k = key(g.workload, g.device, g.task);
    if (!byKey.has(k)) byKey.set(k, g);
  });
  return declared
    .filter(e => accuracyFacetMatches({ device: e.device || '', shortModel: shortModel(e.model) }))
    .map(e => ({
      shortModel: shortModel(e.model),
      device: e.device || '',
      task: e.task,
      group: byKey.get(key(e.workload, e.device, e.task)) || null,
    }));
}

// Tonight's accuracy per model, device and task, on the headline score.
function accuracyTonight() {
  const night = latestNightly();
  const cutoff = windowCutoff();
  const series = ACC_TASKS.map(t => {
    const pts = t.points.filter(p => p.t >= cutoff);
    return { ...t, windowed: pts, verdict: accuracyVerdict(pts.map(p => p.value), THRESHOLDS.accuracy_abs) };
  }).filter(t => t.windowed.length && accuracyFacetMatches(t));

  const expected = expectedAccuracy(accuracyGroups(series));
  if (!expected.length) return null;

  const tonight = [], missing = [];
  expected.forEach(e => {
    if (e.group && reportedTonight(e.group.primary.windowed, night)) tonight.push(e.group);
    else missing.push(e);
  });
  return {
    build: latestNightlyBuild(),
    total: expected.length,
    reported: tonight.length,
    // Overnight only, as perf: a task that skipped the previous nightly
    // changed over more than one night.
    regressed: tonight.filter(g => g.primary.verdict && g.primary.verdict.regressed
      && comparedOvernight(g.primary.windowed, night, previousNightly())).length,
    missing: missing.map(e => ({
      name: e.shortModel + ' · ' + (e.device || '?'),
      task: e.task,
      // Null when nothing reported inside the window: the outage is older
      // than the results the store keeps, so there is no run to link to.
      last: e.group ? e.group.primary.windowed[e.group.primary.windowed.length - 1] : null,
    })),
    threshold: accuracyPoints(THRESHOLDS.accuracy_abs),
  };
}

/* ================================================================
   7b. COVERAGE PANEL
   ================================================================ */
// Below the tabs, not in one, so it shows on every tab.
function renderCoveragePanel() {
  const host = document.getElementById('coverage-host');
  const cov = coverage();
  const accMissing = (cov && cov.accuracy) ? cov.accuracy.missing : [];
  // Whole nightlies after the latest one that produced no AMD results.
  const status = latestRun() ? nightlyGaps() : { known: false, gaps: [] };
  const inBuild = cov ? cov.missing + accMissing.length : 0;
  if (!status.gaps.length && !inBuild) { host.innerHTML = ''; return; }

  const heading = [];
  if (status.gaps.length) {
    heading.push(integer(status.gaps.length) + ' later nightl' + (status.gaps.length === 1 ? 'y' : 'ies')
      + ' with no AMD results');
  }
  if (inBuild) {
    const parts = [];
    if (cov.missing) parts.push(integer(cov.missing) + ' perf config' + (cov.missing === 1 ? '' : 's'));
    if (accMissing.length) {
      parts.push(integer(accMissing.length) + ' accuracy result' + (accMissing.length === 1 ? '' : 's'));
    }
    heading.push('in build #' + esc(cov.build) + ': ' + parts.join(', '));
  }
  // Everything a nightly should have reported, for the whole-nightly rows.
  const expectedAll = cov ? 'all ' + integer(cov.expected) + ' perf configs'
    + (cov.accuracy ? ' and ' + integer(cov.accuracy.total) + ' accuracy results' : '') : 'all results';

  host.innerHTML = '<div class="cov-panel"><div class="cov-head">'
    + '<h3>Missing results</h3><span class="cov-count">' + heading.join(' · ') + '</span>'
    + '</div><div class="cov-list">'
    + status.gaps.map(gap =>
      '<div class="cov-item whole">'
      + '<div class="cov-name">Nightly ' + esc(gap.day) + '</div>'
      + '<div class="cov-shapes">'
      + (gap.runs.length
        ? gap.runs.map(run => '<span class="cov-shape">' + expectedAll + '</span>'
          + '<span class="neutral" style="font-size:11px;margin-left:8px">'
          + buildLink({ build_number: run.build, build_url: safeUrl(run.build_url) }, null) + ' ran</span>').join('')
        : '<span class="cov-shape">' + expectedAll + '</span>')
      + '</div>'
      + '<div class="cov-tag">' + (gap.runs.length ? 'no AMD results'
        : status.known ? 'no nightly ran' : 'no AMD results') + '</div>'
      + '</div>').join('')
    + (cov ? cov.groups.map(g =>
      '<div class="cov-item' + (g.whole ? ' whole' : '') + '">'
      + '<div class="cov-name">' + esc(g.name) + ' <span class="neutral">in #' + esc(cov.build) + '</span></div>'
      +
      // A workload with nothing reported is one line, not one per shape.
      '<div class="cov-shapes">'
      + (g.whole
        ? '<span class="cov-shape">all ' + integer(g.total) + ' configs</span>'
        : g.shapes.map(s => '<span class="cov-shape">' + esc(s) + '</span>').join(''))
      + '</div>'
      + '<div class="cov-tag">'
      + (g.whole ? 'no results' : integer(g.shapes.length) + ' of ' + integer(g.total))
      + '</div>'
      + '</div>').join('') : '')
    + accMissing.map(m =>
      '<div class="cov-item' + (m.last ? '' : ' whole') + '">'
      + '<div class="cov-name">' + esc(m.name) + ' <span class="neutral">in #' + esc(cov.build) + '</span></div>'
      + '<div class="cov-shapes"><span class="cov-shape">' + esc(m.task) + ' accuracy</span>'
      + '<span class="neutral" style="font-size:11px;margin-left:8px">'
      + (m.last
        ? 'last result ' + buildLink(m.last, null) + ' · ' + esc(m.last.day)
        : 'no result in the last ' + WINDOW_DAYS + ' days')
      + '</span></div>'
      + '<div class="cov-tag">no accuracy</div>'
      + '</div>').join('')
    + '</div></div>';
}

/* ================================================================
   8. TABS
   ================================================================ */
const TABS = [
  { id: 'performance', label: 'Performance' },
  { id: 'trends', label: 'Trends' },
  { id: 'tradeoff', label: 'Throughput vs Latency' },
  { id: 'accuracy', label: 'Accuracy' },
  { id: 'data', label: 'Data' },
];

function renderTabs() {
  const host = document.getElementById('tabs-host');
  host.setAttribute('role', 'tablist');
  host.innerHTML = TABS.map(t =>
    '<div class="tab' + (state.tab === t.id ? ' on' : '') + '" data-tab="' + t.id + '" role="tab"'
    + ' aria-selected="' + (state.tab === t.id ? 'true' : 'false') + '">' + esc(t.label)
    + '</div>').join('');
  host.querySelectorAll('.tab').forEach(el => {
    const id = el.getAttribute('data-tab');
    activatable(el, null, () => {
      state.tab = id;
      syncToHash();
      renderAll();
      focusAfterRender('.tab[data-tab="' + id + '"]');
    });
  });
}

/* ================================================================
   9. REGRESSION PANEL
   ================================================================ */


// Grouped by config: one slowdown usually moves several metrics.
function renderRegressionPanel() {
  const shown = new Set(shownConfigs().map(c => c.key));
  const regs = regressionsOf().filter(d => shown.has(d.configKey));
  const below = belowOnly(belowThresholdOf().filter(d => shown.has(d.configKey)), regs).size;
  const thresholdNote = ' · counting drops of <b>' + thresholdLabel() + ' or more</b>'
    + (below ? ' · ' + integer(below) + ' more configuration' + (below === 1 ? '' : 's')
      + ' with only smaller drops' : '');
  // Always the same name; what it found goes in the line beside it.
  const title = '<h3>Overnight regressions</h3>';
  const pair = '#' + esc(latestNightlyBuild() || '?') + ' vs #' + esc(previousNightlyBuild() || '?');
  if (!regs.length) {
    // Counts configs actually compared: one with no previous run was never
    // checked, so the green panel must not include it.
    const compared = new Set(scan().filter(d => shown.has(d.configKey)).map(d => d.configKey)).size;
    if (!compared) {
      return '<div class="reg-panel unknown"><div class="reg-head">'
        + title + '<span class="reg-count"><b>nothing to compare</b>: no configuration ran in both '
        + 'build #' + esc(latestNightlyBuild() || '?') + ' and the nightly before it, #'
        + esc(previousNightlyBuild() || '?') + '</span></div></div>';
    }
    return '<div class="reg-panel clean"><div class="reg-head">'
      + title + '<span class="reg-count"><b>none</b> across ' + integer(compared) + ' compared configuration'
      + (compared === 1 ? '' : 's') + ', ' + pair + thresholdNote + '</span></div></div>';
  }
  const groups = new Map();
  regs.forEach(d => {
    if (!groups.has(d.configKey)) groups.set(d.configKey, { config: d.config, items: [], row: d.row, tier: 0 });
    const g = groups.get(d.configKey);
    g.items.push(d);
    g.tier = Math.max(g.tier, regTier(d.magnitude));
    if (d.row.t > g.row.t) g.row = d.row;
  });
  // Fixed order, so a config is always in the same place.
  const ordered = [...groups.values()].sort((a, b) => compareConfigs(a.config, b.config));

  return '<div class="reg-panel"><div class="reg-head">'
    + title + '<span class="reg-count"><b>' + integer(ordered.length) + ' configuration'
    + (ordered.length === 1 ? '' : 's') + '</b> (' + integer(regs.length) + ' metric drop'
    + (regs.length === 1 ? '' : 's') + '), '
    + pair + thresholdNote
    + '<span class="tier-key">' + REG_TIERS.map(t => '<span class="' + t.cls + '">' + t.label + '</span>').join('')
    + '</span></span></div><div class="reg-list">'
    + ordered.map(g => {
      const linked = !!g.row.build_url;
      const failed = failedNote(g.row);
      const inner = '<span class="reg-config">' + esc(g.config.label) + '</span>'
        + '<span class="reg-metrics">' + g.items.map(d => {
          // Hover shows the actual values.
          const detail = d.metric.label + ': ' + fmt(display(d.latest, d.metric), d.metric.digits)
            + ' vs ' + fmt(display(d.base, d.metric), d.metric.digits) + ' ' + unitOf(d.metric);
          return '<span class="reg-chip ' + REG_TIERS[regTier(d.magnitude)].cls
            + '" title="' + esc(detail) + '">'
            + esc(d.metric.label) + ' <b>' + pctDirection(d.ratio, 1) + '</b></span>';
        }).join('') + '</span>'
        + '<span class="reg-build">'
        + (failed ? '<span class="reg-failed">' + esc(failed) + '</span> · ' : '')
        + 'build #' + esc(g.row.build_number || '?')
        + (linked ? EXT_INLINE : '') + '</span>';
      // The whole row links to the build.
      const tier = REG_TIERS[g.tier].cls;
      return linked
        ? '<a class="reg-item linked ' + tier + '" href="' + esc(g.row.build_url)
        + '" target="_blank" rel="noopener noreferrer">' + inner + '</a>'
        : '<div class="reg-item ' + tier + '">' + inner + '</div>';
    }).join('') + '</div></div>';
}

/* ================================================================
   10. TREND CHARTS
   ================================================================ */
function daysLabel(days) { return 'Last ' + days + ' day' + (days === 1 ? '' : 's'); }
// Each day a chart has a point, with the short commit that point measured,
// newest build winning: a chart labels its own data, not other models'.
function commitsByDay(points) {
  const newest = new Map();
  points.forEach(p => {
    const seen = newest.get(p.day);
    if (!seen || Number(p.build_number) > Number(seen.build_number)) newest.set(p.day, p);
  });
  return new Map([...newest].map(([day, p]) => [day, shortSha(p.vllm_commit).slice(0, 7)]));
}

// An x-axis with every day labelled, none skipped, at noon where nightlies
// sit (min and max are noons). A day with a point reads "9/25 abc1234", as
// ATOM labels it: the date and the commit in two colours. A day without one,
// between the chart's first point and the newest nightly that is due, reads
// "9/24 missing" in orange: no nightly ran, or it ran without these configs.
// A day whose nightly is still going reads "9/29 running" in grey instead: not
// missing, just not finished. Days before the chart's first point or not yet
// due show the date alone.
//
// Returns { scale, labels }: pass labels as a chart plugin. Chart.js draws a
// tick label in one colour, so the scale lays the text out invisibly and the
// plugin paints each piece in its own colour on the same slant.
const DAY_LABEL_ANGLE = 60;

function dayAxis(min, max, theme, points) {
  const commits = commitsByDay(points);
  const first = [...commits.keys()].sort()[0] || '';
  const due = fmtDate(parseTime(DATA.generated_at) - 40 * 3600000);
  const running = new Set(DATA.nightly_runs.filter(isOngoing).map(runDay));
  const isMissing = day => !commits.has(day) && day > first && day <= due;
  const colors = {
    date: cssVar('--text-primary'),
    commit: cssVar('--accent-blue'),
    missing: cssVar('--accent-orange'),
    running: cssVar('--text-secondary'),
    plain: theme.tick,
  };
  // A label as [text, colour] pieces, in reading order.
  const pieces = v => {
    const day = fmtDate(v);
    const [, month, date] = day.split('-');
    const label = Number(month) + '/' + Number(date);
    const sha = commits.get(day);
    if (sha && sha !== '—') return [[label, colors.date], [' ' + sha, colors.commit]];
    if (running.has(day) && day > first) return [[label + ' running', colors.running]];
    if (isMissing(day)) return [[label + ' missing', colors.missing]];
    return [[label, colors.plain]];
  };
  const font = { size: 12 };
  return {
    scale: {
      type: 'linear',
      min: min,
      max: max,
      afterBuildTicks: axis => {
        const ticks = [];
        for (let x = min; x <= max; x += 86400000) ticks.push({ value: x });
        axis.ticks = ticks;
      },
      ticks: {
        font: font,
        autoSkip: false,
        minRotation: DAY_LABEL_ANGLE,
        maxRotation: DAY_LABEL_ANGLE,
        color: 'transparent',
        callback: v => pieces(v).map(piece => piece[0]).join(''),
      },
      grid: { color: theme.grid },
    },
    labels: {
      id: 'dayLabels',
      afterDraw(chart) {
        const scale = chart.scales.x;
        if (!scale) return;
        const ctx = chart.ctx;
        ctx.save();
        ctx.font = font.size + 'px ' + Chart.defaults.font.family;
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        // Where Chart.js starts a slanted label: below the tick mark and its padding.
        const top = scale.top + scale.options.grid.tickLength + scale.options.ticks.padding;
        scale.ticks.forEach(tick => {
          ctx.save();
          ctx.translate(scale.getPixelForValue(tick.value), top);
          ctx.rotate(-DAY_LABEL_ANGLE * Math.PI / 180);
          // Right-aligned at the tick, so draw the last piece first.
          let end = 0;
          pieces(tick.value).slice().reverse().forEach(([text, color]) => {
            ctx.fillStyle = color;
            ctx.fillText(text, end, 0);
            end -= ctx.measureText(text).width;
          });
          ctx.restore();
        });
        ctx.restore();
      },
    },
  };
}

// Presets for the common spans, and a slider for anything between.
function renderWindowBar() {
  const days = chartDays();
  const presets = [...new Set([1, 3, 7, 14, WINDOW_DAYS].filter(d => d <= WINDOW_DAYS))];
  return '<div class="window-bar"><span class="bar-label">Chart window</span>'
    + '<div class="segmented">' + presets.map(d =>
      '<button class="' + (d === days ? 'on' : '') + '" data-days="' + d + '" aria-pressed="'
      + (d === days ? 'true' : 'false') + '" aria-label="Last ' + d + ' day' + (d === 1 ? '' : 's') + '">' + d + 'd</button>').join('')
    + '</div>'
    + '<input type="range" id="chart-days" min="1" max="' + WINDOW_DAYS + '" step="1" value="' + days + '"'
    + ' aria-label="Chart window in days">'
    + '<span class="window-value" id="chart-days-label">' + daysLabel(days) + '</span>'
    + '<span class="window-note">' + (days < WINDOW_DAYS ? 'counted back from the newest nightly · ' : '')
    + 'charts only; regressions always compare the newest nightly with the one before it</span></div>';
}

// Chart legends: large enough to read at a glance, as the charts have room.
function legendLabels() {
  return { color: cssVar('--text-secondary'), font: { size: 12 }, boxWidth: 12, boxHeight: 12, padding: 14 };
}

function chartTheme() {
  return { tick: cssVar('--text-tertiary'), grid: cssVar('--border-light') };
}

// Keep the y-axis at least 1% of the value tall, so float noise between
// two equal runs draws flat instead of as a cliff.
const MIN_Y_SPAN = 0.01;

function floorYSpan(scale) {
  const mid = (scale.max + scale.min) / 2;
  const floor = Math.abs(mid) * MIN_Y_SPAN;
  if (scale.max - scale.min < floor) {
    scale.min = mid - floor / 2;
    scale.max = mid + floor / 2;
  }
}

// The model and device groups that have points to chart for metric, each
// config labelled within its group (shape, and variant where two share one).
function trendGroups(metric) {
  return perfGroups(shownConfigs())
    .map(g => ({
      ...g,
      series: g.configs.map((c, i) => ({ config: c, label: g.labels[i] }))
        .filter(s => chartPointsIn(s.config, metric.key).length),
    }))
    .filter(g => g.series.length);
}

function renderTrendCharts(groups, metric) {
  if (!groups.length) {
    return '<div class="empty">No runs of ' + esc(metric.label) + ' for these filters.</div>';
  }
  const regressed = regressedKeysForMetric(metric.key);
  return '<div class="perf-grid">' + groups.map((g, i) => {
    const regCount = g.series.filter(s => regressed.has(s.config.key)).length;
    return '<div class="perf-card' + (groups.length <= 2 ? ' hero' : '') + (regCount ? ' has-reg' : '') + '">'
      + '<div class="mini-head"><span class="mini-title" style="color:' + esc(g.color) + '">' + esc(g.name) + '</span>'
      + (regCount ? '<span class="mini-reg">' + regCount + ' regressed</span>' : '')
      + '</div><div class="perf-wrap trend-wrap"><canvas id="trend-' + i + '"></canvas></div></div>';
  }).join('') + '</div>';
}

function drawTrendChart(group, canvasId, metric) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const existing = Chart.getChart(canvas);
  if (existing) existing.destroy();

  const theme = chartTheme();
  const bounds = chartBounds();
  const regColor = cssVar('--accent-red') || REG_COLOR;
  // Text colour, so the ring contrasts in both themes.
  const ringColor = cssVar('--text-primary') || '#fff';
  const regForMetric = regressedKeysForMetric(metric.key);
  const failedColor = cssVar('--accent-orange');
  // Each regressed config's change on this metric, to name it in the legend.
  const change = new Map(regressionsOf().filter(d => d.metricKey === metric.key).map(d => [d.configKey, d.ratio]));
  const datasets = group.series
    .map(s => {
      const pts = chartPointsIn(s.config, metric.key);
      if (!pts.length) return null;
      // Red only on the metrics that regressed.
      const isReg = regForMetric.has(s.config.key);
      const color = isReg ? regColor : (BASE_COLOR[s.config.key] || activePalette()[0]);
      const last = pts.length - 1;
      return {
        // Said in words, not only in red: "1K/1K c=1 · regressed ▼1.3%".
        label: s.label + (isReg ? ' · regressed ' + (change.get(s.config.key) > 0 ? '▲' : '▼')
          + Math.abs(change.get(s.config.key) * 100).toFixed(1) + '%' : ''),
        _regressed: isReg,
        _full: s.config.label,
        _key: s.config.key,
        data: pts.map(p => ({ x: dayX(p.day), y: display(p.value, metric), _p: p })),
        borderColor: color,
        backgroundColor: color,
        borderWidth: isReg ? 2.75 : 1.6,
        tension: 0,
        spanGaps: true,
        // Ring the newest point: that is the build to blame. A run with failed
        // requests is an orange triangle: its numbers are not comparable.
        pointStyle: ctx => hadFailures(pts[ctx.dataIndex]) ? 'triangle' : 'circle',
        pointRadius: ctx => hadFailures(pts[ctx.dataIndex]) ? 7
          : (isReg && ctx.dataIndex === last) ? 6 : 2.5,
        pointHoverRadius: 8,
        pointBackgroundColor: ctx => hadFailures(pts[ctx.dataIndex]) ? failedColor : color,
        pointBorderColor: ctx => (isReg && ctx.dataIndex === last) ? ringColor
          : hadFailures(pts[ctx.dataIndex]) ? failedColor : color,
        pointBorderWidth: ctx => (isReg && ctx.dataIndex === last) ? 2 : 0,
      };
    }).filter(Boolean);
  if (!datasets.length) return;

  const xAxis = dayAxis(bounds.min, bounds.max, theme, datasets.flatMap(ds => ds.data.map(d => d._p)));
  new Chart(canvas, {
    type: 'line',
    data: { datasets: datasets },
    plugins: [xAxis.labels],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false, // many charts re-render on every filter change
      interaction: { mode: 'nearest', intersect: false },
      // Several series can share a point: open it when unambiguous, otherwise
      // let the reader pick.
      onClick: (event, _els, chart) => {
        const hits = chart.getElementsAtEventForMode(event, 'point', { intersect: true }, true);
        const candidates = (hits.length ? hits : chart.getElementsAtEventForMode(
            event, 'nearest', { intersect: false }, true))
          .map(hit => {
            const dataset = chart.data.datasets[hit.datasetIndex];
            const point = dataset.data[hit.index];
            return point && point._p
              ? {
                label: dataset._full || dataset.label,
                key: dataset._key,
                color: dataset.borderColor,
                point: point._p,
                value: point.y,
                metric: metric
              }
              : null;
          })
          .filter(Boolean);
        if (!candidates.length) return;
        if (candidates.length === 1) {
          openSeriesHistory(candidates[0]);
          return;
        }
        openPointChooser(metric, candidates);
      },
      scales: {
        x: xAxis.scale,
        y: {
          afterDataLimits: floorYSpan,
          title: { display: !!unitOf(metric), text: unitOf(metric), color: theme.tick, font: { size: 12 } },
          ticks: { color: theme.tick, maxTicksLimit: 7, font: { size: 12 } },
          grid: { color: theme.grid }
        },
      },
      plugins: {
        // Each chart is one model, so its few lines can be named in place;
        // a regressed one in red, as its line is.
        legend: datasets.length <= 12
          ? {
            labels: {
              ...legendLabels(),
              generateLabels: chart => Chart.defaults.plugins.legend.labels.generateLabels(chart).map(item => {
                if (chart.data.datasets[item.datasetIndex]._regressed) item.fontColor = regColor;
                return item;
              }),
            },
          }
          : { display: false },
        tooltip: {
          callbacks: {
            title: items => group.name + ' · ' + items[0].raw._p.day,
            label: item => {
              const p = item.dataset.data[item.dataIndex]._p;
              const sha = p.vllm_commit ? ' · ' + shortSha(p.vllm_commit) : '';
              const note = failedNote(p);
              const failed = note ? ' · ' + note : '';
              return item.dataset.label + ': ' + fmt(item.parsed.y, metric.digits) + ' ' + unitOf(metric) + sha + failed;
            },
            // Nothing else says the points are clickable.
            afterBody: () => '\nClick for history',
          }
        },
      },
    },
  });
}

function renderTrendsTab() {
  const host = document.getElementById('main');
  if (!visibleConfigs().length) {
    host.innerHTML = '<div class="card"><div class="empty">No AMD nightly runs match these filters.</div></div>';
    return;
  }
  const metric = perfMetric();
  const groups = trendGroups(metric);
  host.innerHTML = '<div class="card"><div class="section-head"><h3>' + esc(metric.label)
    + ' over time ' + betterNote(metric) + '</h3>' + metricPicker(metric) + '</div>'
    + '<div class="card-hint">One chart per model and device, a line per configuration. A red line '
    + 'regressed by <b>' + thresholdLabel() + ' or more</b> on this metric, its newest point ringed. '
    + 'Click a point for its history and build.</div>'
    + (groups.some(g => g.series.some(s => chartPointsIn(s.config, metric.key).some(hadFailures)))
      ? '<div class="hint-keys">' + failedKey() + '</div>' : '')
    + renderWindowBar() + renderTrendCharts(groups, metric) + '</div>'
    + renderRegressionPanel();
  bindMetricPicker(host);

  const setDays = days => {
    state.chartDays = days >= WINDOW_DAYS ? null : days;
    syncToHash();
    renderAll();
  };
  host.querySelectorAll('[data-days]').forEach(el => el.addEventListener('click', () => {
    const days = Number(el.getAttribute('data-days'));
    setDays(days);
    focusAfterRender('[data-days="' + days + '"]');
  }));
  const slider = document.getElementById('chart-days');
  if (slider) {
    // The label follows the drag; the charts redraw once, on release.
    slider.addEventListener('input', () => {
      document.getElementById('chart-days-label').textContent = daysLabel(Number(slider.value));
    });
    slider.addEventListener('change', () => {
      setDays(Number(slider.value));
      focusAfterRender('#chart-days');
    });
  }

  groups.forEach((g, i) => drawTrendChart(g, 'trend-' + i, metric));
}

/* ================================================================
   10b. PERFORMANCE TAB
   ================================================================ */
// Each config's newest run in the window: one bar chart per model and device
// (per-GPU numbers do not compare across devices), then a detail table.
const PERF_TABLE_METRICS = ['tput_per_gpu', 'output_tput_per_gpu', 'mean_intvty', 'mean_ttft', 'mean_tpot'];
let MODEL_COLOR = {};

// Assigned over the full dataset, so a series keeps its colour across filters
// and charts. Rebuilt on theme change: each theme has its own palette.
function assignColors() {
  const palette = activePalette();
  // Numbered within each model and device, so the lines of one chart take
  // the palette in order and never share a hue until it runs out.
  BASE_COLOR = {};
  const inGroup = new Map();
  CONFIGS.slice().sort(compareConfigs).forEach(c => {
    const n = inGroup.get(perfGroupKey(c)) || 0;
    inGroup.set(perfGroupKey(c), n + 1);
    BASE_COLOR[c.key] = palette[n % palette.length];
  });
  MODEL_COLOR = Object.fromEntries(
    [...new Set(CONFIGS.map(perfGroupKey))].sort().map((k, i) => [k, palette[i % palette.length]]));
}

// The metric buttons Performance and Trends share; the choice carries over.
function metricPicker(metric) {
  return '<div class="metric-picker">' + METRICS.map(m =>
    '<button class="' + (m.key === metric.key ? 'on' : '') + '" data-perf-metric="' + esc(m.key) + '"'
    + ' aria-pressed="' + (m.key === metric.key ? 'true' : 'false') + '" title="' + betterText(m) + '">'
    + esc(m.label) + ' <span class="better-dir" aria-hidden="true">' + betterArrow(m) + '</span></button>').join('')
    + '</div>';
}

// Which way is good for a metric: ▲ higher is better, ▼ lower is better.
// Shown with the words in headings, since ▲▼ elsewhere mark which way a
// value moved, not whether that was good.
function betterArrow(m) { return m.better === 'higher' ? '▲' : '▼'; }
function betterText(m) { return m.better === 'higher' ? 'higher is better' : 'lower is better'; }
function betterNote(m) {
  return '<span class="better-note">' + betterArrow(m) + ' ' + betterText(m) + '</span>';
}

function bindMetricPicker(host) {
  host.querySelectorAll('[data-perf-metric]').forEach(btn => btn.addEventListener('click', () => {
    const key = btn.getAttribute('data-perf-metric');
    state.perfMetric = key === defaultPerfMetric() ? null : key;
    syncToHash();
    renderAll();
    focusAfterRender('[data-perf-metric="' + cssEscape(key) + '"]');
  }));
}

function defaultPerfMetric() { return METRIC_BY_KEY.tput_per_gpu ? 'tput_per_gpu' : (METRICS[0] || {}).key; }
function perfMetric() { return METRIC_BY_KEY[state.perfMetric] || METRIC_BY_KEY[defaultPerfMetric()]; }
function perfGroupKey(c) { return c.model + '|' + c.device; }


function perfGroups(configs) {
  const groups = new Map();
  configs.forEach(c => {
    const key = perfGroupKey(c);
    if (!groups.has(key)) {
      groups.set(key, {
        key: key,
        name: c.shortModel + ' · ' + String(c.device || '?').toUpperCase(),
        color: MODEL_COLOR[key] || activePalette()[0],
        configs: [],
      });
    }
    groups.get(key).configs.push(c);
  });
  groups.forEach(g => {
    // Two configs at one shape differ by precision or TP; name it on both.
    const count = new Map();
    g.configs.forEach(c => count.set(shapeLabel(c), (count.get(shapeLabel(c)) || 0) + 1));
    g.labels = g.configs.map(c => shapeLabel(c)
      + (count.get(shapeLabel(c)) > 1 ? ' · ' + variantLabel(c) : ''));
  });
  return [...groups.values()];
}

// Newest point, its change against the run before, whether it is tonight's,
// and whether that change is an overnight one.
function latestOf(cfg, metric) {
  const pts = pointsIn(cfg, metric.key);
  if (!pts.length) return null;
  const latest = latestNightly();
  return {
    point: pts[pts.length - 1],
    before: pts.length >= 2 ? pts[pts.length - 2] : null,
    verdict: verdict(pts.map(p => p.value), metric, THRESHOLDS.perf_rel),
    tonight: reportedTonight(pts, latest),
    overnight: comparedOvernight(pts, latest, previousNightly()),
  };
}

// Why a change from tonight's run is not being judged, or '' if it is.
function notOvernightNote(latest) {
  if (!latest || !latest.tonight || latest.overnight || !latest.before) return '';
  return 'vs ' + comparedWhen(latest) + '; skipped the previous run, #' + (previousNightlyBuild() || '?');
}

// Which run a change is against, by run, not by night: nightlies can skip
// days, so the previous run need not be last night's. "the previous run
// (#599)", or "an older run (#586, 9/18)" when the config skipped it.
function comparedWhen(latest) {
  const before = latest.before;
  if (latest.overnight) return 'the previous run (#' + (before.build_number || '?') + ')';
  const [, month, date] = before.day.split('-');
  return 'an older run (#' + (before.build_number || '?') + ', ' + Number(month) + '/' + Number(date) + ')';
}

// Colour a change only when it is overnight and past the threshold. A drop
// takes its size tier, the same yellow/orange/red as the legend and row edge.
function changeClass(latest) {
  if (!latest || !latest.verdict || !latest.overnight) return 'neutral';
  const v = latest.verdict;
  if (v.regressed) return 'chg-' + REG_TIERS[regTier(v.magnitude)].cls;
  if (v.improved && v.magnitude >= THRESHOLDS.perf_rel) return 'good';
  return 'neutral';
}

function changeHtml(latest) {
  if (!latest || !latest.verdict) return '<span class="neutral">—</span>';
  const note = notOvernightNote(latest);
  return '<span class="' + changeClass(latest) + '"'
    + (note ? ' title="' + esc(note) + '"' : '') + '>'
    + pctDirection(latest.verdict.ratio, Math.abs(latest.verdict.ratio) < 0.01 ? 2 : 1) + '</span>';
}

function withAlpha(hex, alpha) {
  const n = parseInt(String(hex).replace('#', ''), 16);
  return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + alpha.toFixed(2) + ')';
}


// Values above the bars, skipped when there are too many to read. Always the
// text colour: the red outline already marks a regression.
// color is one colour, or a function of the bar's index.
function barValuesPlugin(color, format) {
  return {
    id: 'barValues',
    afterDatasetsDraw(chart) {
      const meta = chart.getDatasetMeta(0);
      if (!meta || meta.data.length > 16) return;
      const ctx = chart.ctx;
      ctx.save();
      ctx.font = '10px "SF Mono", Consolas, monospace';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      meta.data.forEach((bar, i) => {
        ctx.fillStyle = typeof color === 'function' ? color(i) : color;
        ctx.fillText(format(chart.data.datasets[0].data[i], i), bar.x, bar.y - 4);
      });
      ctx.restore();
    }
  };
}

// Dashed average line across the chart, labelled at the right.
function avgLinePlugin(avg, label, color) {
  return {
    id: 'avgLine',
    afterDraw(chart) {
      if (avg == null) return;
      const y = chart.scales.y.getPixelForValue(avg);
      if (y < chart.chartArea.top || y > chart.chartArea.bottom) return;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(chart.chartArea.left, y);
      ctx.lineTo(chart.chartArea.right, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = '10px -apple-system, sans-serif';
      ctx.textAlign = 'right';
      ctx.fillText(label, chart.chartArea.right, y - 4);
      ctx.restore();
    }
  };
}

function commitLink(sha) {
  if (!/^[0-9a-f]{7,40}$/i.test(String(sha || ''))) return esc(shortSha(sha));
  return '<a href="https://github.com/vllm-project/vllm/commit/' + esc(sha) + '" target="_blank"'
    + ' rel="noopener noreferrer">' + esc(shortSha(sha)) + EXT_INLINE + '</a>';
}

function renderPerformanceTab() {
  const host = document.getElementById('main');
  // A snapshot of now: configs the newest build's recipes dropped stay in
  // Trends as history, but have no "now" to show here.
  const expectedKeys = new Set(DATA.expected.configs.map(configKeyOf));
  const configs = shownConfigs().filter(c => !isRetired(c, latestNightly(), expectedKeys));
  const metric = perfMetric();
  if (!configs.length || !metric) {
    host.innerHTML = '<div class="card"><div class="empty">No AMD nightly runs match these filters.</div></div>';
    return;
  }
  const groups = perfGroups(configs)
    .map(g => ({ ...g, rows: g.configs.map((c, i) => ({ c: c, label: g.labels[i], latest: latestOf(c, metric) }))
      .filter(r => r.latest) }))
    .filter(g => g.rows.length);

  let html = '<div class="card"><div class="section-head"><h3>' + esc(metric.label)
    + ' ' + betterNote(metric) + '</h3>' + metricPicker(metric) + '</div>'
    // Concurrency is already on the axis (c=). Caveats live in the bar tooltip.
    + '<div class="hint-keys">'
    + '<span class="hint-key"><i class="reg"></i><b>' + thresholdLabel()
    + '+</b> worse vs #' + esc(previousNightlyBuild() || '?') + '</span>'
    + '<span class="hint-key"><i class="absent"></i>not in #' + esc(latestNightlyBuild() || '?')
    + '</span>'
    + (groups.some(g => g.rows.some(r => hadFailures(r.latest.point)))
      ? '<span class="hint-key"><span class="failed-mark">⚠</span>newest run had failed requests: '
        + 'not comparable to a clean run</span>' : '')
    + '</div>';
  // A bar from an older build looks like tonight's except for its shade, so
  // each card also says how many of its configs the newest build lacks.
  const latest = latestNightly();
  const absentBadge = g => {
    const absent = g.rows.filter(r => !r.c.nights.has(latest)).length;
    return absent
      ? '<span class="absent-badge">' + absent + ' of ' + g.rows.length + ' not in #'
        + esc(latestNightlyBuild() || '?') + '</span>'
      : '';
  };
  html += groups.length
    ? '<div class="perf-grid">' + groups.map((g, i) =>
      '<div class="perf-card' + (groups.length <= 2 ? ' hero' : '') + '">'
      + '<div class="perf-card-head"><h4 style="color:' + esc(g.color) + '">' + esc(g.name) + '</h4>'
      + absentBadge(g) + '</div>'
      + '<div class="perf-wrap"><canvas id="perf-' + i + '"></canvas></div></div>').join('') + '</div>'
    : '<div class="empty">No runs of ' + esc(metric.label) + ' for these filters.</div>';
  html += '</div>' + renderPerfTable(configs);
  host.innerHTML = html;

  groups.forEach((g, i) => drawPerfChart(g, 'perf-' + i, metric));
  bindMetricPicker(host);
  bindPerfTable(host, configs);
}

function chartTipEl() {
  let el = document.getElementById('chart-tip');
  if (!el) {
    el = document.createElement('div');
    el.id = 'chart-tip';
    el.className = 'chart-tip';
    el.setAttribute('role', 'tooltip');
    document.body.appendChild(el);
  }
  return el;
}

function hideChartTip() {
  const el = document.getElementById('chart-tip');
  if (el) el.classList.remove('on');
}

function placeChartTip(el, chart, tooltip) {
  const rect = chart.canvas.getBoundingClientRect();
  const tw = el.offsetWidth, th = el.offsetHeight;
  let left = rect.left + tooltip.caretX - tw / 2;
  let top = rect.top + tooltip.caretY - th - 12;
  left = Math.min(Math.max(8, left), window.innerWidth - tw - 8);
  if (top < 8) top = rect.top + tooltip.caretY + 14;
  el.style.left = left + 'px';
  el.style.top = top + 'px';
}

function tipRow(k, v, vCls) {
  return '<div class="tip-k">' + esc(k) + '</div>'
    + '<div class="tip-v' + (vCls ? ' ' + vCls : '') + '">' + v + '</div>';
}

function perfBarTipHtml(row, metric, unit) {
  const latest = row.latest, p = latest.point;
  let rows = tipRow('Build', '#' + esc(p.build_number || '?'));
  rows += tipRow('vLLM', esc(shortSha(p.vllm_commit)));
  const prevNightly = '#' + (previousNightlyBuild() || '?');
  const note = text => { rows += '<div class="tip-note">' + esc(text) + '</div>'; };
  const days = integer(WINDOW_DAYS);
  if (latest.verdict && latest.before) {
    rows += tipRow('Change', esc(pct(latest.verdict.ratio, 2) + ' vs ' + comparedWhen(latest)),
      changeClass(latest));
    if (notOvernightNote(latest)) note('Skipped the previous run, ' + prevNightly);
  } else if (latest.before) {
    rows += tipRow('Change', 'Not available', 'neutral');
    note('The run before it, #' + (latest.before.build_number || '?') + ', has no usable value');
  } else if (latest.tonight) {
    rows += tipRow('Change', 'No previous run', 'warnc');
    note('Nothing to compare against: this is its first run in the last ' + days
      + ' days, and it was not in ' + prevNightly);
  } else {
    rows += tipRow('Change', 'No previous run', 'neutral');
    note('Nothing to compare against: its only run in the last ' + days + ' days');
  }
  if (!latest.tonight) {
    rows += tipRow('Status', 'Not in #' + esc(latestNightlyBuild() || '?'));
  }
  if (failedNote(p)) rows += tipRow('Failed', esc(failedNote(p)), 'warnc');
  return '<div class="tip-title">' + esc(row.label) + '</div>'
    + '<div class="tip-value">' + esc(fmt(display(p.value, metric), metric.digits)) + '</div>'
    + '<div class="tip-unit">' + esc(unit) + '</div>'
    + '<div class="tip-rows">' + rows + '</div>'
    + '<div class="tip-foot">Click for history</div>';
}

function perfBarExternalTooltip(group, metric) {
  const unit = unitOf(metric);
  return context => {
    const tip = chartTipEl();
    const tooltip = context.tooltip;
    if (!tooltip.opacity || !tooltip.dataPoints || !tooltip.dataPoints.length) {
      tip.classList.remove('on');
      return;
    }
    const row = group.rows[tooltip.dataPoints[0].dataIndex];
    if (!row) { tip.classList.remove('on'); return; }
    tip.innerHTML = perfBarTipHtml(row, metric, unit);
    tip.classList.add('on');
    placeChartTip(tip, context.chart, tooltip);
  };
}

function drawPerfChart(group, canvasId, metric) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const theme = chartTheme();
  const regColor = cssVar('--accent-red') || REG_COLOR;
  const valueColor = cssVar('--text-secondary');
  const regKeys = regressedKeysForMetric(metric.key);
  const concs = [...new Set(group.rows.map(r => Number(r.c.conc) || 0))].sort((a, b) => a - b);
  // Darker with concurrency, as ATOM shades it; faded when not from tonight.
  // A tint that reads on near-black washes out on white, so light mode starts
  // higher and spans further.
  const shade = isLightTheme()
    ? { faded: 0.22, single: 0.75, lo: 0.5, span: 0.4 }
    : { faded: 0.12, single: 0.55, lo: 0.3, span: 0.35 };
  const alphaOf = r => {
    if (!r.latest.tonight) return shade.faded;
    if (concs.length <= 1) return shade.single;
    return shade.lo + concs.indexOf(Number(r.c.conc) || 0) / (concs.length - 1) * shade.span;
  };
  const values = group.rows.map(r => display(r.latest.point.value, metric));
  const avg = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  const unit = unitOf(metric);
  new Chart(canvas, {
    type: 'bar',
    data: {
      labels: group.rows.map(r => r.label),
      datasets: [{
        data: values,
        backgroundColor: group.rows.map(r => withAlpha(group.color, alphaOf(r))),
        borderColor: group.rows.map(r => regKeys.has(r.c.key) ? regColor : group.color),
        borderWidth: group.rows.map(r => regKeys.has(r.c.key) ? 2.5 : 1.5),
        borderRadius: 4,
      }]
    },
    plugins: [
      barValuesPlugin(
        i => hadFailures(group.rows[i].latest.point) ? cssVar('--accent-orange') : valueColor,
        (v, i) => (hadFailures(group.rows[i].latest.point) ? '⚠ ' : '') + compact(v, metric.digits)),
      avgLinePlugin(avg, 'avg ' + compact(avg, metric.digits), theme.tick),
    ],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      layout: { padding: { top: 22 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          enabled: false,
          external: perfBarExternalTooltip(group, metric),
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: theme.tick, maxRotation: 45, font: { size: 10 } } },
        y: {
          beginAtZero: true,
          grace: '5%',
          title: { display: !!unit, text: unit, color: theme.tick },
          ticks: { color: theme.tick, font: { size: 10 } },
          grid: { color: theme.grid },
        },
      },
      onClick: (_event, elements) => {
        if (elements.length) openConfigHistory(group.rows[elements[0].index].c, metric);
      },
    },
  });
}

// A short column heading for a metric: "Total tput" for "Total Throughput".
function shortMetricLabel(m) { return m.label.replace('Throughput', 'tput'); }

function renderPerfTable(configs) {
  const selected = perfMetric();
  // The headline metrics, plus the one picked above when it is not among them.
  const cols = [...new Set([...PERF_TABLE_METRICS, selected.key])].map(k => METRIC_BY_KEY[k]).filter(Boolean);
  const regs = regressionsOf();
  const worstTier = new Map();
  regs.forEach(d => worstTier.set(d.configKey, Math.max(worstTier.has(d.configKey) ? worstTier.get(d.configKey) : -1, regTier(d.magnitude))));
  const latest = configs.map(c => Object.fromEntries(METRICS.map(m => [m.key, latestOf(c, m)])));

  // Throughput gets a bar against its model and device's largest; latency a
  // shade from that model and device's fastest (green) to slowest (red).
  // Across models the numbers differ by orders of magnitude.
  const ranges = new Map();
  configs.forEach((c, i) => cols.forEach(m => {
    const entry = latest[i][m.key];
    if (!entry) return;
    const key = perfGroupKey(c) + '|' + m.key;
    const r = ranges.get(key) || { min: Infinity, max: -Infinity };
    ranges.set(key, { min: Math.min(r.min, entry.point.value), max: Math.max(r.max, entry.point.value) });
  }));
  const heat = r => {
    const x = Math.min(1, Math.max(0, r));
    if (x < 0.33) return 'rgba(109,191,128,' + (0.08 + x * 0.2).toFixed(2) + ')';
    if (x < 0.67) return 'rgba(227,199,90,' + (0.08 + (x - 0.33) * 0.2).toFixed(2) + ')';
    return 'rgba(242,92,78,' + (0.08 + (x - 0.67) * 0.3).toFixed(2) + ')';
  };
  const cell = (c, m, l) => {
    const entry = l[m.key];
    if (!entry) return '<td class="num neutral">—</td>';
    // Each value with its own change against the previous run beneath it.
    const text = esc(fmt(display(entry.point.value, m), m.digits))
      + '<span class="cell-change">' + changeHtml(entry) + '</span>';
    const rg = ranges.get(perfGroupKey(c) + '|' + m.key);
    if (m.better === 'higher' && /tput/.test(m.key)) {
      const w = rg.max > 0 ? entry.point.value / rg.max * 100 : 0;
      return '<td class="num data-bar"><div class="data-bar-fill" style="width:' + w.toFixed(1)
        + '%;background:' + (m.key === 'tput_per_gpu' ? 'rgba(109,191,128,0.22)' : 'rgba(136,153,184,0.2)')
        + '"></div><span class="data-bar-text">' + text + '</span></td>';
    }
    if (m.better === 'lower') {
      const r = rg.max > rg.min ? (entry.point.value - rg.min) / (rg.max - rg.min) : 0;
      return '<td class="num" style="background:' + heat(r) + '">' + text + '</td>';
    }
    return '<td class="num">' + text + '</td>';
  };

  let html = '<div class="card"><div class="section-head"><h3>Detail table</h3></div>'
    // Row tint is the worst regression on any metric. Bars and latency shades
    // are a separate scale: position within the row's model and device.
    + '<div class="hint-keys">'
    + '<span class="hint-key"><i class="edge minor"></i>worst metric, ' + REG_TIERS[0].label + '</span>'
    + '<span class="hint-key"><i class="edge moderate"></i>' + REG_TIERS[1].label + '</span>'
    + '<span class="hint-key"><i class="edge major"></i>' + REG_TIERS[2].label + '</span>'
    + '<span class="hint-key"><i class="heat"></i>latency, fastest to slowest for that model and device</span>'
    + '<span class="hint-key"><i class="absent"></i>not in #' + esc(latestNightlyBuild() || '?') + '</span>'
    + '</div>'
    + '<div class="table-scroll"><table class="perf-table"><thead><tr>'
    + '<th>Model</th><th>Shape</th><th>Variant</th>'
    + cols.map(m => '<th class="num' + (m.key === selected.key ? ' picked' : '') + '" title="' + esc(m.label)
      + ', with its change vs #' + esc(previousNightlyBuild() || '?') + '">' + esc(shortMetricLabel(m))
      + (unitOf(m) ? '<br><span class="neutral">' + esc(unitOf(m)) + '</span>' : '') + '</th>').join('')
    + '<th>Build</th></tr></thead><tbody>';

  configs.forEach((c, i) => {
    const l = latest[i];
    const head = l[selected.key] || l[defaultPerfMetric()] || Object.values(l).find(Boolean);
    if (!head) return;
    const tier = worstTier.has(c.key) ? REG_TIERS[worstTier.get(c.key)].cls : '';
    const cls = (tier ? 'reg-' + tier : '') + (head.tonight ? '' : ' stale');
    html += '<tr data-row="' + i + '" class="' + cls.trim() + '"'
      + (head.tonight ? '' : ' title="Not in build #' + esc(latestNightlyBuild() || '?') + '"') + '>'
      + '<td class="model">' + (tier ? '⚠ ' : '') + esc(c.shortModel)
      + ' <span class="device">' + esc(String(c.device || '?').toUpperCase()) + '</span></td>'
      + '<td>' + esc(shapeLabel(c)) + '</td>'
      + '<td>' + esc(variantLabel(c) || '—') + '</td>'
      + cols.map(m => cell(c, m, l)).join('')
      + '<td>' + buildLink(head.point, null) + '</td></tr>'
      + '<tr class="detail-row" data-detail="' + i + '"><td colspan="' + (4 + cols.length) + '">'
      + perfDetail(c, l, head.point) + '</td></tr>';
  });
  return html + '</tbody></table></div></div>';
}

function perfDetail(c, latest, p) {
  const metrics = METRICS.filter(m => latest[m.key]).map(m =>
    '<div class="detail-item clickable" data-hist="' + esc(m.key) + '"><span class="detail-key">' + esc(m.label)
    + '</span><span class="detail-val">' + esc(fmt(display(latest[m.key].point.value, m), m.digits))
    + ' ' + esc(unitOf(m)) + ' ' + changeHtml(latest[m.key]) + '</span></div>').join('');
  const item = (k, v) => '<div class="detail-item"><span class="detail-key">' + esc(k)
    + '</span><span class="detail-val">' + v + '</span></div>';
  const requests = p.completed_requests != null
    ? integer(Math.round(p.completed_requests)) + ' completed'
      + (p.failed_requests > 0 ? ', <span class="warnc">' + integer(Math.round(p.failed_requests)) + ' failed</span>' : '')
    : '—';
  return '<div class="detail-box"><div><h5>Metrics · click for history</h5>' + metrics + '</div>'
    + '<div><h5>Run</h5>'
    + item('Configuration', esc(c.label))
    + item('Device', esc(String(c.device || '?').toUpperCase()))
    + item('Parallelism', esc(c.parallel_label))
    + item('Precision', esc(c.precision || 'unstated'))
    + item('Date', esc(p.day))
    + item('Build', buildLink(p, null))
    + item('vLLM commit', commitLink(p.vllm_commit))
    + item('Image', esc(p.image || '—'))
    + item('Requests', requests)
    + '</div></div>';
}

function bindPerfTable(host, configs) {
  host.querySelectorAll('.perf-table tr[data-row]').forEach(row => {
    const i = row.getAttribute('data-row');
    activatable(row, null, event => {
      if (event.target && event.target.closest && event.target.closest('a')) return;
      const detail = host.querySelector('tr[data-detail="' + i + '"]');
      if (!detail) return;
      detail.classList.toggle('open');
      row.classList.toggle('open');
    });
  });
  host.querySelectorAll('.perf-table tr.detail-row').forEach(detail => {
    const config = configs[Number(detail.getAttribute('data-detail'))];
    detail.querySelectorAll('[data-hist]').forEach(el => {
      const metric = METRIC_BY_KEY[el.getAttribute('data-hist')];
      if (config && metric) activatable(el, 'Open history for ' + config.label + ' ' + metric.label,
        () => openConfigHistory(config, metric));
    });
  });
}

/* ================================================================
   10c. THROUGHPUT VS LATENCY
   ================================================================ */
// ATOM's tradeoff tab, on this payload: one curve per shape (ISL/OSL, TP,
// precision), points are concurrency. Axes are already in the metric registry
// (interactivity = 1/TPOT, total tok/s/GPU). Pair both from the same nightly
// so a missing latency does not borrow throughput from another build.
function matchPoint(haystack, needle) {
  if (!haystack.length || !needle) return null;
  if (needle.night != null) {
    const hit = haystack.find(p => p.night === needle.night);
    if (hit) return hit;
  }
  return haystack.find(p => p.t === needle.t) || null;
}

function latestPaired(cfg, xKey, yKey) {
  const xs = pointsIn(cfg, xKey), ys = pointsIn(cfg, yKey);
  for (let i = xs.length - 1; i >= 0; i--) {
    const y = matchPoint(ys, xs[i]);
    if (y) return { x: xs[i], y: y };
  }
  return null;
}

function tradeoffPoint(cfg) {
  const tputKey = 'tput_per_gpu';
  let paired = latestPaired(cfg, 'mean_intvty', tputKey);
  let intvty = paired ? paired.x.value : null;
  if (!paired) {
    paired = latestPaired(cfg, 'mean_tpot', tputKey);
    if (paired && paired.x.value > 0) intvty = 1 / paired.x.value;
  }
  if (!paired || !(intvty > 0) || !isFinite(paired.y.value)) return null;
  const tpotPaired = latestPaired(cfg, 'mean_tpot', tputKey);
  return {
    cfg: cfg,
    intvty: intvty,
    tput: paired.y.value,
    tpot: tpotPaired ? tpotPaired.x.value : null,
    point: paired.y,
  };
}

function tradeoffCurveKey(c) {
  return [c.model, c.device, c.precision, c.parallel_label, c.isl, c.osl].join('|');
}

function concLabelPlugin(id) {
  return {
    id: id,
    afterDatasetsDraw(chart) {
      const ctx = chart.ctx;
      ctx.save();
      ctx.font = '8px -apple-system, sans-serif';
      chart.data.datasets.forEach((ds, di) => {
        if (!ds._concLabels) return;
        const meta = chart.getDatasetMeta(di);
        ctx.fillStyle = ds.borderColor;
        meta.data.forEach((pt, pi) => {
          if (ds.data[pi] == null) return;
          ctx.fillText('c' + ds._concLabels[pi], pt.x + 5, pt.y - 5);
        });
      });
      ctx.restore();
    }
  };
}

function renderTradeoffTab() {
  const host = document.getElementById('main');
  const configs = shownConfigs();
  const tputMetric = METRIC_BY_KEY.tput_per_gpu;
  const intvtyMetric = METRIC_BY_KEY.mean_intvty;
  const tpotMetric = METRIC_BY_KEY.mean_tpot;
  if (!configs.length || !tputMetric) {
    host.innerHTML = '<div class="card"><div class="empty">No AMD nightly runs match these filters.</div></div>';
    return;
  }
  const rows = configs.map(tradeoffPoint).filter(Boolean);
  if (!rows.length) {
    host.innerHTML = '<div class="card"><div class="empty">No configuration has both throughput and latency in this window.</div></div>';
    return;
  }
  const intvtyLabel = intvtyMetric ? intvtyMetric.label : 'Interactivity';

  const groups = perfGroups(rows.map(r => r.cfg)).map(g => {
    const groupRows = rows.filter(r => perfGroupKey(r.cfg) === g.key);
    const shapeCount = new Map();
    groupRows.forEach(r => {
      const shape = String(r.cfg.isl) + '/' + String(r.cfg.osl);
      if (!shapeCount.has(shape)) shapeCount.set(shape, new Set());
      shapeCount.get(shape).add(tradeoffCurveKey(r.cfg));
    });
    const curves = new Map();
    groupRows.forEach(r => {
      const key = tradeoffCurveKey(r.cfg);
      if (!curves.has(key)) {
        const c = r.cfg;
        const collide = (shapeCount.get(String(c.isl) + '/' + String(c.osl)) || new Set()).size > 1;
        curves.set(key, {
          key: key,
          label: lenLabel(c.isl) + '/' + lenLabel(c.osl)
            + (collide ? ' · ' + variantLabel(c) : ''),
          color: BASE_COLOR[c.key] || g.color,
          points: [],
        });
      }
      curves.get(key).points.push(r);
    });
    curves.forEach(curve => curve.points.sort((a, b) => (a.cfg.conc || 0) - (b.cfg.conc || 0)));
    return { ...g, curves: [...curves.values()].filter(c => c.points.length) };
  }).filter(g => g.curves.length);

  let html = '<div class="card"><h3>Throughput vs latency</h3></div>';

  groups.forEach((g, i) => {
    html += '<div class="tradeoff-pair">'
      + '<div class="perf-card"><h4 style="color:' + esc(g.color) + '">' + esc(g.name)
      + ' — ' + esc(intvtyLabel) + ' vs ' + esc(tputMetric.label) + '</h4>'
      + '<div class="perf-wrap"><canvas id="tradeoff-hero-' + i + '"></canvas></div></div>'
      + '<div class="perf-card"><h4 style="color:' + esc(g.color) + '">' + esc(g.name)
      + ' — Concurrency Scaling</h4>'
      + '<div class="perf-wrap"><canvas id="tradeoff-scale-' + i + '"></canvas></div></div>'
      + '</div>';
  });

  html += renderTradeoffHeatmaps(rows);
  host.innerHTML = html;
  groups.forEach((g, i) => {
    drawTradeoffScatter(g, 'tradeoff-hero-' + i, tputMetric, intvtyMetric);
    drawConcurrencyScaling(g, 'tradeoff-scale-' + i, tputMetric, tpotMetric);
  });
}

function renderTradeoffHeatmaps(rows) {
  const tputMetric = METRIC_BY_KEY.tput_per_gpu;
  const byModel = new Map();
  rows.forEach(r => {
    const mk = perfGroupKey(r.cfg);
    if (!byModel.has(mk)) {
      byModel.set(mk, { name: r.cfg.shortModel + ' · ' + String(r.cfg.device || '?').toUpperCase(),
        color: MODEL_COLOR[mk] || activePalette()[0], cells: new Map(), shapes: new Set(), concs: new Set() });
    }
    const g = byModel.get(mk);
    const shape = lenLabel(r.cfg.isl) + '/' + lenLabel(r.cfg.osl);
    g.shapes.add(shape);
    g.concs.add(Number(r.cfg.conc) || 0);
    const cellKey = shape + '|' + r.cfg.conc;
    const prev = g.cells.get(cellKey);
    if (!prev || r.tput > prev.tput) g.cells.set(cellKey, r);
  });
  const blocks = [...byModel.values()].filter(g => g.concs.size >= 2 && g.cells.size >= 3);
  if (!blocks.length) {
    return '<div class="card"><div class="empty">Not enough concurrency levels for a throughput heatmap '
      + '(need 2+ conc and 3+ cells).</div></div>';
  }
  let html = '<div class="card"><h3>Throughput matrix <span class="better-note">'
    + esc(unitOf(tputMetric)) + ', newest run; greener is higher within a model</span></h3>'
    + '<div class="matrix-grid">';
  blocks.forEach(g => {
    const concs = [...g.concs].sort((a, b) => a - b);
    const shapes = [...g.shapes].sort();
    const vals = [...g.cells.values()].map(r => display(r.tput, tputMetric)).filter(v => v != null);
    const minV = Math.min(...vals), maxV = Math.max(...vals), range = maxV - minV || 1;
    html += '<div class="perf-card"><h4 style="color:' + esc(g.color) + '">' + esc(g.name) + '</h4>'
      + '<table class="heatmap-table"><thead><tr><th></th>'
      + concs.map(c => '<th>c=' + c + '</th>').join('') + '</tr></thead><tbody>';
    shapes.forEach(shape => {
      html += '<tr><td class="row-label">' + esc(shape) + '</td>';
      concs.forEach(c => {
        const cell = g.cells.get(shape + '|' + c);
        if (!cell) { html += '<td class="neutral">—</td>'; return; }
        const v = display(cell.tput, tputMetric);
        const ratio = (v - minV) / range;
        html += '<td style="background:hsla(' + Math.round(ratio * 120)
          + ',50%,40%,0.35)">' + esc(compact(v, tputMetric.digits)) + '</td>';
      });
      html += '</tr>';
    });
    html += '</tbody></table></div>';
  });
  return html + '</div></div>';
}

function drawTradeoffScatter(group, canvasId, tputMetric, intvtyMetric) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const theme = chartTheme();
  const regColor = cssVar('--accent-red') || REG_COLOR;
  const ringColor = cssVar('--text-primary') || '#fff';
  const regKeys = tputMetric ? regressedKeysForMetric(tputMetric.key) : new Set();
  const datasets = group.curves.map(curve => {
    const data = curve.points.map(r => {
      // Interactivity is derived when the registry has no series for it, in
      // which case there is no metric to scale against.
      const scaled = display(r.intvty, intvtyMetric || {});
      return {
        x: scaled == null ? r.intvty : scaled,
        y: display(r.tput, tputMetric),
        _r: r,
      };
    }).sort((a, b) => a.x - b.x);
    return {
      label: curve.label,
      data: data,
      borderColor: curve.color,
      backgroundColor: curve.color,
      borderWidth: 2,
      pointRadius: ctx => regKeys.has(data[ctx.dataIndex]._r.cfg.key) ? 6 : 5,
      pointHoverRadius: 7,
      pointBackgroundColor: curve.color,
      pointBorderColor: ctx => regKeys.has(data[ctx.dataIndex]._r.cfg.key) ? ringColor : curve.color,
      pointBorderWidth: ctx => regKeys.has(data[ctx.dataIndex]._r.cfg.key) ? 2 : 0,
      showLine: true,
      fill: false,
      tension: 0,
      _concLabels: data.map(p => p._r.cfg.conc),
    };
  }).filter(ds => ds.data.length);
  if (!datasets.length) return;
  new Chart(canvas, {
    type: 'scatter',
    data: { datasets: datasets },
    plugins: [concLabelPlugin('tradeoffConc-' + canvasId)],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      layout: { padding: { top: 8, right: 18 } },
      plugins: {
        legend: datasets.length <= 8
          ? { labels: legendLabels() }
          : { display: false },
        tooltip: {
          callbacks: {
            title: items => {
              const p = datasets[items[0].datasetIndex].data[items[0].dataIndex]._r;
              return group.name + ' · ' + datasets[items[0].datasetIndex].label + ' · c=' + p.cfg.conc;
            },
            label: item => [
              (intvtyMetric ? intvtyMetric.label : 'Interactivity') + ': '
                + fmt(item.parsed.x, intvtyMetric ? intvtyMetric.digits : 1)
                + ' ' + (intvtyMetric ? unitOf(intvtyMetric) : 'tok/s/user'),
              tputMetric.label + ': ' + fmt(item.parsed.y, tputMetric.digits) + ' ' + unitOf(tputMetric),
            ],
            afterBody: items => {
              const p = items[0].raw._r.point;
              return ['Build #' + (p.build_number || '?') + ' · vLLM ' + shortSha(p.vllm_commit), '', 'Click for history'];
            },
          }
        },
      },
      scales: {
        x: {
          title: { display: true, text: axisTitle(intvtyMetric, 'Interactivity (tok/s/user)'), color: theme.tick },
          ticks: { color: theme.tick, font: { size: 10 } },
          grid: { color: theme.grid },
          grace: '5%',
        },
        y: {
          // grace pads both ends; beginAtZero is what holds the floor at 0.
          beginAtZero: true,
          title: { display: true, text: axisTitle(tputMetric), color: theme.tick },
          ticks: { color: theme.tick, font: { size: 10 } },
          grid: { color: theme.grid },
          grace: '5%',
        },
      },
      onClick: (_e, els, chart) => {
        if (!els.length) return;
        const ds = chart.data.datasets[els[0].datasetIndex];
        const p = ds.data[els[0].index]._r;
        if (p) openConfigHistory(p.cfg, tputMetric);
      },
    },
  });
}

function drawConcurrencyScaling(group, canvasId, tputMetric, tpotMetric) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const theme = chartTheme();
  const dashPatterns = [[], [6, 3], [2, 2], [8, 4, 2, 4]];
  const datasets = [];
  const concs = new Set();
  group.curves.forEach((curve, i) => {
    const dash = dashPatterns[i % dashPatterns.length];
    const tputData = curve.points.map(r => {
      concs.add(Number(r.cfg.conc) || 0);
      return { x: r.cfg.conc, y: display(r.tput, tputMetric), _r: r };
    }).sort((a, b) => a.x - b.x);
    datasets.push({
      label: curve.label + ' Tput',
      data: tputData,
      borderColor: curve.color,
      backgroundColor: curve.color,
      borderWidth: 2,
      borderDash: dash,
      pointRadius: 4,
      pointHoverRadius: 6,
      showLine: true,
      fill: false,
      tension: 0,
      yAxisID: 'y',
    });
    if (tpotMetric) {
      const tpotData = curve.points.filter(r => r.tpot != null).map(r => ({
        x: r.cfg.conc, y: display(r.tpot, tpotMetric), _r: r
      })).sort((a, b) => a.x - b.x);
      if (tpotData.length) {
        datasets.push({
          label: curve.label + ' TPOT',
          data: tpotData,
          borderColor: withAlpha(curve.color, 0.85),
          backgroundColor: curve.color,
          borderWidth: 2,
          borderDash: dash,
          pointRadius: 3,
          pointHoverRadius: 5,
          pointStyle: 'triangle',
          showLine: true,
          fill: false,
          tension: 0,
          yAxisID: 'y2',
        });
      }
    }
  });
  if (!datasets.length) return;
  const allConcs = [...concs].sort((a, b) => a - b);
  const useLog = allConcs.length > 1 && Math.min(...allConcs) > 0
    && Math.max(...allConcs) / Math.min(...allConcs) >= 4;
  new Chart(canvas, {
    type: 'scatter',
    data: { datasets: datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: datasets.length <= 8
          ? { labels: { ...legendLabels(), usePointStyle: true } }
          : { display: false },
        tooltip: {
          callbacks: {
            title: items => {
              const p = items[0].raw._r;
              return group.name + ' · ' + items[0].dataset.label.replace(/ (Tput|TPOT)$/, '')
                + ' · c=' + p.cfg.conc;
            },
            label: item => item.dataset.yAxisID === 'y2'
              ? tpotMetric.label + ': ' + fmt(item.parsed.y, tpotMetric.digits) + ' ' + unitOf(tpotMetric)
              : tputMetric.label + ': ' + fmt(item.parsed.y, tputMetric.digits) + ' ' + unitOf(tputMetric),
            afterBody: () => '\nClick for history',
          }
        },
      },
      scales: {
        x: {
          type: useLog ? 'logarithmic' : 'linear',
          title: { display: true, text: 'Concurrency', color: theme.tick },
          ticks: {
            color: theme.tick,
            font: { size: 10 },
            callback: v => allConcs.includes(v) ? v : '',
          },
          afterBuildTicks: axis => { axis.ticks = allConcs.map(v => ({ value: v })); },
          grid: { color: theme.grid },
        },
        y: {
          position: 'left',
          beginAtZero: true,
          title: { display: true, text: axisTitle(tputMetric), color: theme.tick },
          ticks: { color: theme.tick, font: { size: 10 } },
          grid: { color: theme.grid },
          grace: '5%',
        },
        y2: {
          position: 'right',
          beginAtZero: true,
          title: { display: true, text: axisTitle(tpotMetric), color: theme.tick },
          ticks: { color: theme.tick, font: { size: 10 } },
          grid: { drawOnChartArea: false },
          grace: '5%',
        },
      },
      onClick: (_e, els, chart) => {
        if (!els.length) return;
        const p = chart.data.datasets[els[0].datasetIndex].data[els[0].index]._r;
        if (p) openConfigHistory(p.cfg, tputMetric);
      },
    },
  });
}

/* ================================================================
   11. ACCURACY TAB
   ================================================================ */
function renderAccuracyTab() {
  const host = document.getElementById('main');
  const cutoff = windowCutoff();
  const series = ACC_TASKS.map(t => {
    const pts = t.points.filter(p => p.t >= cutoff);
    return { ...t, windowed: pts, verdict: accuracyVerdict(pts.map(p => p.value), THRESHOLDS.accuracy_abs) };
  }).filter(t => t.windowed.length && accuracyFacetMatches(t));
  const groups = accuracyGroups(series);

  if (!groups.length) {
    host.innerHTML = '<div class="card"><div class="empty">No accuracy results in this window.</div></div>';
    return;
  }
  const threshold = accuracyPoints(THRESHOLDS.accuracy_abs);
  let html = '<div class="card"><h3>Accuracy by model</h3>'
    + '<div class="card-hint">Scores from lm-eval, as the percentage of questions answered '
    + 'correctly; higher is better. Each is compared against <em>the previous nightly</em>, '
    + 'not against a reference score for the model. A change of <b>' + threshold
    + ' or more</b> counts as improved or regressed; smaller moves are normal run-to-run '
    + 'variation (one gsm8k question is about 0.08 pt). Click a row for history.</div>'
    + '<div class="table-scroll"><table><thead><tr>'
    + '<th>Model</th><th>Device</th><th>Task</th><th class="num">Score</th>'
    +
    // "Previous nightly", not "Baseline", which in eval work means a reference score.
    '<th class="num">Previous nightly</th><th class="num">Change</th><th>Status</th>'
    + '<th>Other scores</th><th class="num">Runs</th><th>Build</th>'
    + '</tr></thead><tbody>';
  const tonight = latestNightly(),
    previous = previousNightly();
  groups.forEach((g, i) => {
    const s = g.primary,
      v = s.verdict,
      newest = s.windowed[s.windowed.length - 1];
    // Judged only as an overnight change, like the Accuracy card.
    const current = reportedTonight(s.windowed, tonight);
    const overnight = comparedOvernight(s.windowed, tonight, previous);
    const cls = !v || !v.counted || !overnight ? 'neutral' : (v.improved ? 'good' : 'bad');
    html += '<tr class="clickable" data-acc="' + i + '">'
      + '<td class="config">' + esc(g.shortModel) + '</td>'
      + '<td>' + esc(g.device || '—') + '</td>'
      + '<td>' + esc(g.task) + '</td>'
      + '<td class="num">' + esc(accuracyPercent(newest.value))
      + '<br><span class="neutral" style="font-size:11px">' + esc(accuracyMetricLabel(s.metric)) + '</span></td>'
      + '<td class="num">' + (v ? esc(accuracyPercent(v.base)) : '—') + '</td>'
      + '<td class="num ' + cls + ' mono">' + (v ? esc(accuracyPoints(v.delta, true)) : '—') + '</td>'
      + '<td>' + (!current ? '<span class="pill">not in #' + esc(nightLabel(tonight) || '?') + '</span>'
        : !v ? '<span class="pill">first run</span>'
        : !overnight ? '<span class="pill">skipped #' + esc(nightLabel(previous) || '?') + '</span>'
        : v.regressed ? '<span class="pill alert">regressed</span>'
        : v.counted ? '<span class="pill ok">improved</span>'
        : '<span class="pill">within ' + esc(threshold) + '</span>') + '</td>'
      + '<td class="neutral" style="font-size:11px">' + g.others.map(o => {
        const ov = o.verdict,
          last = o.windowed[o.windowed.length - 1];
        return esc(accuracyMetricLabel(o.metric)) + ' ' + esc(accuracyPercent(last.value))
          + (ov ? ' (' + esc(accuracyPoints(ov.delta, true)) + ')' : '');
      }).join('<br>') + '</td>'
      + '<td class="num">' + s.windowed.length + '</td>'
      + '<td>' + buildLink(newest, null) + '</td></tr>';
  });
  html += '</tbody></table></div></div>';
  host.innerHTML = html;

  host.querySelectorAll('tr[data-acc]').forEach(row => {
    const g = groups[Number(row.getAttribute('data-acc'))];
    if (!g) return;
    const s = g.primary;
    activatable(row, 'Open history for ' + g.shortModel + ' ' + g.device + ' ' + g.task, event => {
      // Let a build link inside the row navigate instead of opening history.
      if (event.target && event.target.closest && event.target.closest('a')) return;
      openHistory(g.shortModel + ' · ' + (g.device || '?') + ' — ' + g.task,
        accuracyMetricLabel(s.metric) + ' · higher is better · compared against the previous nightly',
        ACCURACY_METRIC, s.windowed, s.verdict);
    });
  });
}

// Accuracy has no shape, precision or concurrency: only Device and Model apply.
function accuracyFacetMatches(t) {
  return ['device', 'model'].every(key => {
    const sel = state.selected[key];
    return sel.size === 0 || sel.has(key === 'device' ? t.device : t.shortModel);
  });
}

// Shown as a percentage; changes in points, so +0.15 pt is not read as relative.
const ACCURACY_METRIC = {
  label: 'Accuracy',
  unit: '%',
  display_scale: 100,
  digits: 2,
  better: 'higher',
  absolute: true
};



/* ================================================================
   12. DATA TAB
   ================================================================ */
// Every nightly of every shown configuration, one row each, as ATOM's Data &
// Trace tab. Sortable, and copyable as CSV in the order shown. The newest
// value per config, with every metric's change, is the Performance table.
const DEFAULT_RUNS_SORT = '-day';

function runColumns() {
  const metrics = PERF_TABLE_METRICS.map(k => METRIC_BY_KEY[k]).filter(Boolean);
  return [
    { key: 'day', label: 'Date', get: r => r.point.day, csv: [['nightly_date', r => r.point.day]] },
    { key: 'build', label: 'Build', num: true, get: r => Number(r.build),
      html: r => buildLink(r.point, null), csv: [['build', r => r.build], ['build_url', r => r.point.build_url]] },
    { key: 'model', label: 'Model', get: r => r.config.shortModel, csv: [['model', r => r.config.model]] },
    { key: 'device', label: 'Device', get: r => r.config.device },
    { key: 'shape', label: 'ISL/OSL', get: r => lenLabel(r.config.isl) + '/' + lenLabel(r.config.osl),
      csv: [['isl', r => r.config.isl], ['osl', r => r.config.osl]] },
    { key: 'conc', label: 'Conc', num: true, get: r => Number(r.config.conc) },
    { key: 'parallel', label: 'Parallel', get: r => r.config.parallel_label },
    { key: 'precision', label: 'Precision', get: r => r.config.precision,
      html: r => esc(r.config.precision || 'unstated') },
    ...metrics.map(m => ({
      key: m.key, label: m.label, unit: unitOf(m), num: true,
      get: r => display(r.values[m.key], m),
      html: r => esc(fmt(display(r.values[m.key], m), m.digits)),
      csv: [[m.label + (unitOf(m) ? ' (' + unitOf(m) + ')' : ''), r => display(r.values[m.key], m)]],
    })),
    { key: 'failed', label: 'Failed', num: true, get: r => r.point.failed_requests,
      html: r => r.point.failed_requests == null ? '—'
        : r.point.failed_requests > 0 ? '<span class="warnc">' + integer(r.point.failed_requests) + '</span>' : '0',
      csv: [['completed_requests', r => r.point.completed_requests], ['failed_requests', r => r.point.failed_requests]] },
    { key: 'vllm', label: 'vLLM', get: r => r.point.vllm_commit, html: r => commitLink(r.point.vllm_commit),
      csv: [['vllm_commit', r => r.point.vllm_commit]] },
  ];
}

function sortedRuns(columns) {
  const spec = state.runsSort || DEFAULT_RUNS_SORT;
  const descending = spec.startsWith('-');
  const column = columns.find(c => c.key === spec.replace(/^-/, '')) || columns[0];
  const rows = runRows(shownConfigs(), PERF_TABLE_METRICS, (c, key) => pointsIn(c, key));
  // Ties keep a stable, readable order: newest nightly, then config.
  const settled = sortRows(sortRows(rows, r => r.config.label, false), r => r.point.t, true);
  return { rows: sortRows(settled, column.get, descending), key: column.key, descending };
}

function runsCsv(columns, rows) {
  const cells = columns.flatMap(c => c.csv || [[c.key, c.get]]);
  return toCsv([cells.map(c => c[0]), ...rows.map(r => cells.map(c => c[1](r)))]);
}

function renderDataTab() {
  const host = document.getElementById('main');
  const columns = runColumns();
  const sorted = sortedRuns(columns);
  if (!sorted.rows.length) {
    host.innerHTML = '<div class="card"><div class="empty">No AMD nightly runs match these filters.</div></div>';
    return;
  }
  const arrow = c => c.key !== sorted.key ? '' : (sorted.descending ? ' ▾' : ' ▴');
  host.innerHTML = '<div class="card"><div class="section-head"><h3>Every run</h3>'
    + '<button class="btn" id="copy-csv">Copy CSV (' + integer(sorted.rows.length) + ' rows)</button></div>'
    + '<div class="card-hint">Every nightly of every configuration in the last ' + WINDOW_DAYS
    + ' days, one row each. The filters above apply; click a heading to sort, a row for its history. '
    + 'Copy CSV copies these rows in this order, with full commits and build links.</div>'
    + '<div class="table-scroll"><table class="runs-table"><thead><tr>'
    + columns.map(c => '<th' + (c.num ? ' class="num"' : '') + ' aria-sort="'
      + (c.key !== sorted.key ? 'none' : sorted.descending ? 'descending' : 'ascending') + '">'
      + '<button class="sort-btn" data-sort="' + esc(c.key) + '">' + esc(c.label) + arrow(c)
      + (c.unit ? '<br><span class="neutral">' + esc(c.unit) + '</span>' : '') + '</button></th>').join('')
    + '</tr></thead><tbody>'
    + sorted.rows.map((r, i) => '<tr class="clickable" data-run="' + i + '">'
      + columns.map(c => '<td' + (c.num ? ' class="num"' : '') + '>'
        + (c.html ? c.html(r) : esc(c.get(r) == null ? '—' : c.get(r))) + '</td>').join('')
      + '</tr>').join('')
    + '</tbody></table></div></div>';

  host.querySelectorAll('[data-sort]').forEach(btn => btn.addEventListener('click', () => {
    const key = btn.getAttribute('data-sort');
    const column = columns.find(c => c.key === key);
    // A new column starts at its most useful end: biggest numbers, A to Z.
    const next = key === sorted.key ? (sorted.descending ? '' : '-') + key
      : (column && column.num ? '-' : '') + key;
    state.runsSort = next === DEFAULT_RUNS_SORT ? null : next;
    syncToHash();
    renderAll();
    focusAfterRender('[data-sort="' + cssEscape(key) + '"]');
  }));
  host.querySelectorAll('tr[data-run]').forEach(tr => {
    const r = sorted.rows[Number(tr.getAttribute('data-run'))];
    activatable(tr, 'Open history for ' + r.config.label, event => {
      if (event.target && event.target.closest && event.target.closest('a')) return;
      openConfigHistory(r.config, perfMetric());
    });
  });
  const copy = document.getElementById('copy-csv');
  copy.addEventListener('click', () => {
    const label = copy.textContent;
    const done = text => { copy.textContent = text; setTimeout(() => { copy.textContent = label; }, 1500); };
    if (!(navigator.clipboard && navigator.clipboard.writeText)) { done('Copy failed'); return; }
    navigator.clipboard.writeText(runsCsv(columns, sorted.rows)).then(() => done('Copied'), () => done('Copy failed'));
  });
}

/* ================================================================
   13. HISTORY OVERLAY
   ================================================================ */
function openHistory(title, subtitle, metric, points, v) {
  // The chooser hides the chart; restore it in case we arrived from there.
  document.getElementById('history-chart-wrap').style.display = '';
  document.getElementById('overlay-title').textContent = title;
  document.getElementById('overlay-sub').textContent = subtitle;

  const unit = unitOf(metric);
  const items = [
    { k: 'Latest', v: fmt(display(points[points.length - 1].value, metric), metric.digits) + ' ' + unit },
    { k: 'Previous', v: v ? fmt(display(v.base, metric), metric.digits) + ' ' + unit : '—' },
    { k: 'Change', v: !v ? '—' : (metric.absolute ? accuracyPoints(v.delta, true) : pct(v.ratio, 2)) },
    { k: 'Runs', v: String(points.length) },
  ];
  document.getElementById('overlay-summary').innerHTML = items.map(i =>
    '<div class="item"><div class="k">' + esc(i.k) + '</div><div class="v">' + esc(i.v) + '</div></div>').join('');

  const rows = points.slice().reverse();
  const table = document.getElementById('overlay-table');
  table.innerHTML =
    '<table><thead><tr><th>Date</th><th class="num">Value</th><th>vLLM commit</th>'
    + '<th>Image</th><th>Build</th></tr></thead><tbody>'
    + rows.map((p, i) => '<tr' + (p.build_url ? ' class="clickable" data-row="' + i + '"' : '') + '>'
      + '<td>' + esc(p.day) + '</td>'
      + '<td class="num">' + esc(fmt(display(p.value, metric), metric.digits)) + '</td>'
      + '<td class="mono">' + esc(shortSha(p.vllm_commit)) + '</td>'
      + '<td class="mono">' + esc(p.image || '—') + '</td>'
      + '<td>' + buildLink(p, null) + '</td></tr>').join('')
    + '</tbody></table>';

  // The whole row opens the build; the link stays for middle- and ctrl-click.
  table.querySelectorAll('tr[data-row]').forEach(row => {
    const point = rows[Number(row.getAttribute('data-row'))];
    if (!point) return;
    activatable(row, 'Open build #' + (point.build_number || '?') + ' in Buildkite', event => {
      // The link navigates itself; handling it here too would open it twice.
      if (event.target && event.target.closest && event.target.closest('a')) return;
      window.open(point.build_url, '_blank', 'noopener');
    });
  });

  const canvas = document.getElementById('history-chart');
  if (historyChart) { historyChart.destroy();
    historyChart = null; }
  const theme = chartTheme();
  const accent = cssVar(metric.better === 'higher' ? '--accent-blue' : '--ui');
  // A day either side of a single run, so it has an axis to sit on.
  const xAxis = dayAxis(dayX(points[0].day) - (points.length > 1 ? 0 : 86400000),
    dayX(points[points.length - 1].day) + (points.length > 1 ? 0 : 86400000), theme, points);
  historyChart = new Chart(canvas, {
    type: 'line',
    plugins: [xAxis.labels],
    data: {
      datasets: [{
        label: metric.label + (unit ? ' (' + unit + ')' : ''),
        data: points.map(p => ({ x: dayX(p.day), y: display(p.value, metric), _p: p })),
        borderColor: accent,
        backgroundColor: accent,
        borderWidth: 2,
        pointStyle: ctx => hadFailures(points[ctx.dataIndex]) ? 'triangle' : 'circle',
        pointRadius: ctx => hadFailures(points[ctx.dataIndex]) ? 7 : 3,
        pointBackgroundColor: ctx => hadFailures(points[ctx.dataIndex]) ? cssVar('--accent-orange') : accent,
        pointBorderColor: ctx => hadFailures(points[ctx.dataIndex]) ? cssVar('--accent-orange') : accent,
        tension: 0,
        fill: false,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: xAxis.scale,
        y: { afterDataLimits: floorYSpan, ticks: { color: theme.tick, font: { size: 10 } }, grid: { color: theme.grid } },
      },
      plugins: {
        legend: { labels: { color: cssVar('--text-secondary') } },
        tooltip: {
          callbacks: {
            title: items => items[0].raw._p.day,
            afterBody: items => {
              const p = points[items[0].dataIndex] || {};
              const lines = ['vLLM commit: ' + shortSha(p.vllm_commit), 'Build: #' + (p.build_number || '?')];
              return failedNote(p) ? lines.concat(failedNote(p)) : lines;
            },
          }
        },
      },
    },
  });
  showOverlay();
}
// A config's history for one metric, over the window.
function openConfigHistory(config, metric) {
  const points = pointsIn(config, metric.key);
  if (!points.length) return;
  openHistory(config.label + ' — ' + metric.label,
    (metric.better === 'higher' ? 'higher is better' : 'lower is better') + ' · vs the previous nightly',
    metric, points, verdict(points.map(p => p.value), metric, THRESHOLDS.perf_rel));
}

// Open the full history of the clicked series.
function openSeriesHistory(candidate) {
  // By key, not label: labels leave out TP, precision and the model's org.
  const config = visibleConfigs().filter(c => c.key === candidate.key)[0];
  const metric = candidate.metric;
  if (!config) {
    // Stale render with no matching config: show just the clicked point.
    openHistory(candidate.label + ' — ' + metric.label,
      candidate.point.day || '—', metric, [candidate.point], null);
    return;
  }
  openConfigHistory(config, metric);
}

// Several series on one point: list them and let the reader choose.
function openPointChooser(metric, candidates) {
  if (historyChart) { historyChart.destroy();
    historyChart = null; }
  document.getElementById('history-chart-wrap').style.display = 'none';
  document.getElementById('overlay-summary').innerHTML = '';
  document.getElementById('overlay-title').textContent =
    candidates.length + ' configurations at this point';
  document.getElementById('overlay-sub').textContent =
    metric.label + ' · ' + (candidates[0].point.day || '—')
    + ' · select one to see its history';

  document.getElementById('overlay-table').innerHTML = '<div class="pick-list">'
    + candidates.map((c, index) =>
      '<button class="pick-item" data-pick="' + index + '">'
      + '<span class="pick-swatch" style="background:' + esc(c.color) + '"></span>'
      + '<span class="pick-label">' + esc(c.label) + '</span>'
      + '<span class="pick-value">' + esc(fmt(c.value, metric.digits)) + ' '
      + esc(unitOf(metric)) + '</span></button>').join('') + '</div>';

  document.getElementById('overlay-table').querySelectorAll('button[data-pick]')
    .forEach(button => button.addEventListener('click', () => {
      const candidate = candidates[Number(button.getAttribute('data-pick'))];
      if (candidate) openSeriesHistory(candidate);
    }));

  showOverlay();
}

// Move focus into the dialog on open, and back to the opener on close.
let overlayOpener = null;

function showOverlay() {
  const overlay = document.getElementById('overlay');
  if (!overlay.classList.contains('open')) overlayOpener = document.activeElement;
  overlay.classList.add('open');
  document.getElementById('overlay-close').focus();
}

function closeHistory() {
  const overlay = document.getElementById('overlay');
  const wasOpen = overlay.classList.contains('open');
  overlay.classList.remove('open');
  if (historyChart) { historyChart.destroy();
    historyChart = null; }
  if (wasOpen && overlayOpener && document.contains(overlayOpener)) overlayOpener.focus();
  if (wasOpen) overlayOpener = null;
}

/* ================================================================
   14. HASH STATE
   ================================================================ */
let suppressHash = false;

function syncToHash() {
  const parts = [];
  if (state.tab !== 'performance') parts.push('tab=' + state.tab);
  if (perfMetric().key !== defaultPerfMetric()) parts.push('metric=' + perfMetric().key);
  if (chartDays() !== WINDOW_DAYS) parts.push('days=' + chartDays());
  FACETS.forEach(f => {
    const sel = [...state.selected[f.key]];
    if (sel.length) parts.push(f.key + '=' + sel.map(encodeURIComponent).join(','));
  });
  if (state.onlyRegressed) parts.push('regressed=1');
  if (state.onlyFailed) parts.push('failed=1');
  if (state.runsSort) parts.push('sort=' + encodeURIComponent(state.runsSort));
  suppressHash = true;
  window.location.hash = parts.join('&');
  setTimeout(() => { suppressHash = false; }, 0);
}

function readFromHash() {
  const raw = (window.location.hash || '').replace(/^#/, '');
  state.tab = 'performance';
  state.perfMetric = null;
  state.chartDays = null;
  state.runsSort = null;
  FACETS.forEach(f => state.selected[f.key].clear());
  state.onlyRegressed = false;
  state.onlyFailed = false;
  raw.split('&').forEach(chunk => {
    const i = chunk.indexOf('=');
    if (i < 0) return;
    const k = chunk.slice(0, i),
      v = chunk.slice(i + 1);
    if (k === 'tab' && TABS.some(t => t.id === v)) state.tab = v;
    else if (k === 'days' && /^\d+$/.test(v)) state.chartDays = Number(v);
    else if (k === 'regressed') state.onlyRegressed = v === '1';
    else if (k === 'failed') state.onlyFailed = v === '1';
    else if (k === 'sort') state.runsSort = safeDecode(v) || null;
    else if (k === 'metric' && METRIC_BY_KEY[v]) state.perfMetric = v;
    else {
      const facet = FACETS.find(f => f.key === k);
      if (facet) {
        v.split(',').map(safeDecode).filter(Boolean)
          .forEach(val => state.selected[k].add(val));
      }
    }
  });
  invalidateScan();
}
// A truncated or hand-edited link must not take the page down with it.
function safeDecode(value) {
  try { return decodeURIComponent(value); } catch (_e) { return ''; }
}

/* ================================================================
   15. RENDER ROOT
   ================================================================ */
function renderAll() {
  // Close any open drill-down: it would describe the previous view.
  closeHistory();
  hideChartTip();
  _chartBounds = null;
  renderNotice();
  renderHeaderFilters();
  renderKpis();
  renderCoveragePanel();
  renderTabs();
  // Chart.js keeps every chart it made; free the ones about to be replaced.
  document.querySelectorAll('#main canvas').forEach(c => {
    const chart = Chart.getChart(c);
    if (chart) chart.destroy();
  });
  if (state.tab === 'performance') renderPerformanceTab();
  else if (state.tab === 'tradeoff') renderTradeoffTab();
  else if (state.tab === 'trends') renderTrendsTab();
  else if (state.tab === 'accuracy') renderAccuracyTab();
  else renderDataTab();
}

function relTime(ms) {
  const diff = Date.now() - ms;
  if (diff < 60000) return 'just now';
  if (diff < 3600000) return Math.round(diff / 60000) + 'm ago';
  if (diff < 86400000) return Math.round(diff / 3600000) + 'h ago';
  const days = Math.round(diff / 86400000);
  return days + (days === 1 ? ' day ago' : ' days ago');
}

// Relative, as ATOM shows it; the exact UTC time is on hover.
function renderUpdated() {
  const el = document.getElementById('generated-at');
  const t = parseTime(DATA.generated_at);
  if (!t) { el.textContent = 'Update time unknown'; return; }
  el.textContent = 'Updated ' + relTime(t);
  el.title = 'Generated ' + new Date(t).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

function renderChrome() {
  renderUpdated();
  // Keeps "Updated 5m ago" true while the page stays open.
  setInterval(renderUpdated, 60000);
  const url = safeUrl(DATA.pipeline.url);
  document.getElementById('pipeline-link').href = url;
  document.getElementById('pipeline-link-footer').href = url;
  const parts = ['Showing the last ' + WINDOW_DAYS + ' days'];
  const nightlies = nightliesInWindow();
  if (nightlies) parts.push(nightlies + (nightlies === 1 ? ' nightly' : ' nightlies'));
  document.getElementById('retention-note').textContent = parts.join(' · ');
}

// The icon says which mode the button switches to, and the label has to say
// the same thing. CSS swaps the icon but cannot write an aria-label.
function syncThemeLabel() {
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  const label = light ? 'Switch to dark mode' : 'Switch to light mode';
  const btn = document.getElementById('theme-toggle');
  btn.setAttribute('aria-label', label);
  btn.title = label;
}

function wireChrome() {
  syncThemeLabel();
  document.getElementById('theme-toggle').addEventListener('click', () => {
    const light = document.documentElement.getAttribute('data-theme') === 'light';
    if (light) {
      document.documentElement.removeAttribute('data-theme');
      localStorage.setItem('perf-eval-theme', 'dark');
    } else {
      document.documentElement.setAttribute('data-theme', 'light');
      localStorage.setItem('perf-eval-theme', 'light');
    }
    syncThemeLabel();
    assignColors();
    // Chart.js reads colours when a chart is built, so redraw on theme change.
    renderAll();
  });
  document.getElementById('copy-link').addEventListener('click', () => {
    const btn = document.getElementById('copy-link');
    const restore = () => { btn.textContent = 'Copy link'; };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(window.location.href).then(
        () => { btn.textContent = 'Copied';
          setTimeout(restore, 1500); },
        () => { btn.textContent = 'Copy failed';
          setTimeout(restore, 1500); });
    } else { btn.textContent = 'Copy failed';
      setTimeout(restore, 1500); }
  });
  document.getElementById('overlay-close').addEventListener('click', closeHistory);
  document.getElementById('overlay').addEventListener('click', e => {
    if (e.target === document.getElementById('overlay')) closeHistory();
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    closeHistory();
    closeHeaderFilters();
  });
  document.addEventListener('click', closeHeaderFilters);
  window.addEventListener('hashchange', () => {
    if (suppressHash) return;
    readFromHash();
    renderAll();
  });
}

function fail(msg) {
  document.getElementById('main').innerHTML =
    '<div class="card"><div class="empty">' + esc(msg) + '</div></div>';
  document.getElementById('generated-at').textContent = 'Data unavailable';
}

// Before the fetch, so the label is right even when the payload never loads.
syncThemeLabel();

const { deriveKey, openWith, exportKey, importKey } = window.PerfUnlock;
// The key, not the password, is kept, and only for this tab.
const KEY_STORE = 'perf-eval-key';

// The payload, once someone signs in. A key kept from earlier in the tab is
// tried first; it stops working when the login changes.
async function unlock(envelope) {
  const kept = sessionStorage.getItem(KEY_STORE);
  if (kept) {
    try { return await openWith(envelope, await importKey(kept)); }
    catch (e) { sessionStorage.removeItem(KEY_STORE); }
  }
  const form = document.getElementById('login-form');
  const error = document.getElementById('login-error');
  const submit = document.getElementById('login-submit');
  document.getElementById('generated-at').textContent = 'Sign in to view';
  form.hidden = false;
  document.getElementById('login-user').focus();
  return new Promise(resolve => {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      error.textContent = '';
      submit.disabled = true;
      try {
        const key = await deriveKey(document.getElementById('login-user').value,
          document.getElementById('login-pass').value, envelope);
        const payload = await openWith(envelope, key);
        sessionStorage.setItem(KEY_STORE, await exportKey(key));
        form.hidden = true;
        resolve(payload);
      } catch (err) {
        error.textContent = 'Wrong username or password.';
      } finally {
        submit.disabled = false;
      }
    });
  });
}

document.getElementById('sign-out').addEventListener('click', () => {
  sessionStorage.removeItem(KEY_STORE);
  location.reload();
});

// The build step rewrites this path with a content-hash cache tag.
fetch('perf_eval.sealed.json')
  .then(r => {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  })
  .then(unlock)
  .then(payload => {
    document.body.classList.remove('locked');
    // Decrypted here, so the download is made here rather than linked.
    document.getElementById('download-data').href =
      URL.createObjectURL(new Blob([JSON.stringify(payload)], { type: 'application/json' }));
    // Built and deployed with this page, so its shape is aggregate.py's.
    DATA = payload;
    THRESHOLDS = DATA.thresholds;

    // aggregate.py owns the window and guarantees the payload covers it.
    const windowDays = (DATA.retention || {}).display_window_days;
    if (Number.isFinite(windowDays) && windowDays > 0) WINDOW_DAYS = windowDays;

    // Metric order comes from the payload.
    METRICS = Object.keys(DATA.metric_meta)
      .filter(k => k !== 'accuracy')
      .map(k => ({ key: k, ...DATA.metric_meta[k] }))
      .sort((a, b) => (a.order == null ? 99 : a.order) - (b.order == null ? 99 : b.order));
    METRIC_BY_KEY = Object.fromEntries(METRICS.map(m => [m.key, m]));

    CONFIGS = buildConfigs(DATA);
    ACC_TASKS = buildAccuracy(DATA);
    assignColors();

    readFromHash();
    renderChrome();
    wireChrome();
    renderAll();
  })
  .catch(err => fail('Could not load the data: ' + err.message));
})();
