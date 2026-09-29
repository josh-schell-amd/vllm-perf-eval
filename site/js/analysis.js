// The page's pure logic: formatting, dates, and the rules for what counts
// as an overnight change. No DOM and no page state, so tests/js runs it in
// Node. A plain script: the page reads window.PerfAnalysis, Node requires it.
(function (root) {
'use strict';

// Upstream strings go into markup, so everything interpolated goes through esc().
function esc(v) {
  return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// HTML escaping still allows javascript: URLs, so only http(s) passes.
function safeUrl(v) {
  const url = String(v || '').trim();
  return /^https?:\/\//i.test(url) ? url : '';
}

function integer(v) { return Number(v || 0).toLocaleString('en-US'); }

function fmt(v, d) { return v == null || !isFinite(v) ? '—' : Number(v).toFixed(d == null ? 1 : d); }

function shortModel(m) { return String(m || '').split('/').pop(); }

function shortSha(s) { return s ? String(s).slice(0, 8) : '—'; }

function pct(r, d) { return r == null ? '—' : (r > 0 ? '+' : '') + (r * 100).toFixed(d == null ? 1 : d) + '%'; }

// The arrow shows which way the number moved, not whether that is good: a
// latency regression is ▲, a throughput regression ▼.
function pctDirection(r, d) {
  if (r == null) return '—';
  const arrow = r > 0 ? '▲' : (r < 0 ? '▼' : '');
  return (arrow ? '<span class="dir">' + arrow + '</span>' : '')
    + Math.abs(r * 100).toFixed(d == null ? 1 : d) + '%';
}

function median(nums) {
  if (!nums.length) return null;
  const s = [...nums].sort((a, b) => a - b),
    mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

// Safari will not parse "YYYY-MM-DD HH:MM:SS" without the T and Z.
function parseTime(s) {
  const t = String(s || '').trim();
  if (!t) return 0;
  const ms = Date.parse(t.includes('T') ? t : t.replace(' ', 'T') + 'Z');
  return Number.isFinite(ms) ? ms : 0;
}

function fmtDate(ms) {
  const d = new Date(ms);
  return isNaN(d) ? '—' : d.toISOString().slice(0, 10);
}

// The date Buildkite names a nightly by ("Nightly run 2026-09-26"), which can
// differ from the day it finished; the finish day only if it names none.
function nightlyDay(p) {
  return /^\d{4}-\d{2}-\d{2}$/.test(p.nightly_date || '') ? p.nightly_date : fmtDate(parseTime(p.date));
}

// Charts place a nightly at noon on its named day, so the axis dates match.
function dayX(day) {
  const ms = Date.parse(day + 'T12:00:00Z');
  return Number.isFinite(ms) ? ms : 0;
}

// Newest value against the one before it. perfRel is the smallest relative
// move that counts (payload thresholds.perf_rel).
function verdict(values, metric, perfRel) {
  if (values.length < 2) return null;
  const latest = values[values.length - 1];
  // Newest vs the run before it, with no smoothing: perf-eval already medians
  // repeated runs, so averaging again would only blur the change.
  const base = values[values.length - 2];
  if (base == null || !isFinite(base) || base === 0) return null;
  const ratio = (latest - base) / Math.abs(base);
  const improved = metric.better === 'higher' ? ratio > 0 : ratio < 0;
  const magnitude = Math.abs(ratio);
  return {
    latest: latest,
    base: base,
    ratio: ratio,
    improved: improved,
    magnitude: magnitude,
    // ratio !== 0 keeps a flat value neutral if the threshold is ever zero.
    regressed: ratio !== 0 && !improved && magnitude >= perfRel,
    points: values.length
  };
}

// Absolute change: accuracy is on 0..1, so changes read in points.
// accuracyAbs is payload thresholds.accuracy_abs.
function accuracyVerdict(values, accuracyAbs) {
  if (values.length < 2) return null;
  const latest = values[values.length - 1];
  const base = values[values.length - 2];
  if (base == null || !isFinite(base)) return null;
  const delta = latest - base;
  const improved = delta > 0;
  const magnitude = Math.abs(delta);
  // Float tolerance: 0.9334 - 0.9234 is not exactly 0.01.
  const counted = delta !== 0 && magnitude >= accuracyAbs - 1e-9;
  return {
    latest: latest,
    base: base,
    delta: delta,
    ratio: base ? delta / Math.abs(base) : null,
    improved: improved,
    counted: counted,
    magnitude: magnitude,
    regressed: counted && !improved,
    points: values.length
  };
}

// True when the config's newest point is from the latest nightly. Otherwise
// its last two points are an older night's change, not tonight's.
// Which nightly a build belongs to, from its payload builds entry. Mirrors
// store.nightly_identity: a rebuild of a nightly (same day, same commit) is
// the same nightly, so a partial retry does not hide the configs it skipped.
function nightKey(b, build) {
  return b.nightly_date || b.vllm_commit
    ? 'nightly:' + (b.nightly_date || '') + ':' + (b.vllm_commit || '')
    : 'build:' + build;
}

// latest and previous are nightKeys; points carry theirs as `night`.
function reportedTonight(pts, latest) {
  return pts.length > 0 && pts[pts.length - 1].night === latest;
}

// Tonight's run, and the run before it is the previous nightly. A config that
// skipped the previous run compares against an older one, so its change spans every
// night it missed and is not an overnight change.
function comparedOvernight(pts, latest, previous) {
  return pts.length >= 2 && reportedTonight(pts, latest)
    && previous != null && pts[pts.length - 2].night === previous;
}

// Tier by the displayed 1-decimal value, so "2.5%" is always yellow.
const REG_TIERS = [
  { cls: 'minor', max: 2.5, label: 'up to 2.5%' },
  { cls: 'moderate', max: 5, label: 'up to 5%' },
  { cls: 'major', max: Infinity, label: 'over 5%' },
];

function regTier(magnitude) {
  const shown = Number((magnitude * 100).toFixed(1));
  return REG_TIERS.findIndex(t => shown <= t.max);
}

// Coverage: did the configs in the recipes the latest nightly ran (payload
// `expected`) report? Expected comes from the recipes, not recent data, so a
// workload broken for weeks, or never working, stays missing.
function configKeyOf(parts) {
  return [parts.model, parts.device, parts.precision, parts.parallel_label, parts.isl, parts.osl, parts.conc].join('|');
}

function lenLabel(v) {
  if (v == null) return '?';
  return v >= 1024 && v % 1024 === 0 ? (v / 1024) + 'K' : String(v);
}

function shapeLabel(c) { return lenLabel(c.isl) + '/' + lenLabel(c.osl) + ' c=' + c.conc; }

// What tells apart two configs of one shape: precision and parallelism. An
// unnamed precision is left out rather than shown as a placeholder.
function variantLabel(c) { return [c.precision, c.parallel_label].filter(Boolean).join(' '); }

function compact(v, digits) {
  if (v == null || !isFinite(v)) return '—';
  return Math.abs(v) >= 10000 ? (v / 1000).toFixed(1) + 'K' : fmt(v, digits);
}

// "exact_match,flexible-extract" -> "flexible extract".
function accuracyMetricLabel(metric) {
  const [name, filter] = String(metric).split(',');
  const text = (filter && filter !== 'none') ? filter : name;
  return text.replace(/[_-]+/g, ' ');
}

function accuracyPercent(v) { return v == null || !isFinite(v) ? '—' : (v * 100).toFixed(2) + '%'; }

function accuracyPoints(delta, signed) {
  if (delta == null || !isFinite(delta)) return '—';
  const pts = delta * 100;
  return (signed && pts > 0 ? '+' : '') + (+pts.toFixed(2)) + ' pt';
}

const DAY_MS = 86400000;

// Nightlies newest first, by Buildkite day then build number: a retried
// nightly shares its day. dayByBuild maps a build number to dayX of its day.
// nights: nightKey -> { x: its day, builds: [build numbers] }. Newest day
// first, then the newest build for a shared day.
function rankNightlies(nights) {
  const newest = n => Math.max(...n.builds.map(Number));
  return [...nights.entries()]
    .sort((a, b) => b[1].x - a[1].x || newest(b[1]) - newest(a[1]))
    .map(e => e[0]);
}

// A nightly named for day D usually finishes by 14:00 UTC on D+1, so it
// counts as missing from 16:00 UTC that day: 40 hours after D began.
const NIGHTLY_DUE_MS = 40 * 3600000;

function runDay(run) { return run.nightly_date || String(run.date || '').slice(0, 10); }

// The days after latest, the newest nightly with AMD results ({ build, day }),
// that have none: each due day up to collectedAt, and any day with a newer
// nightly that produced none. Each gap lists that day's nightly builds; an
// empty list means no nightly ran, when runs (payload nightly_runs) are
// recorded at all (known), or an unknown cause when they are not. Newest first.
function nightlyStatus(runs, latest, collectedAt) {
  const known = runs.length > 0;
  if (!latest) return { known: known, gaps: [] };
  // A later build on the latest day is a rebuild of that nightly, which
  // already has results.
  const empty = runs.filter(r => !r.amd_results && runDay(r) > latest.day);
  const days = new Set(empty.map(runDay));
  for (let x = dayX(latest.day) + DAY_MS; x <= dayX(fmtDate(collectedAt - NIGHTLY_DUE_MS)); x += DAY_MS) {
    days.add(fmtDate(x));
  }
  const gaps = [...days].sort().reverse()
    .map(day => ({ day: day, runs: empty.filter(r => runDay(r) === day) }));
  return { known: known, gaps: gaps };
}

// One row per nightly of each config, with its values on metricKeys.
// pointsOf(config, key) gives a config's points for one metric.
function runRows(configs, metricKeys, pointsOf) {
  const rows = [];
  configs.forEach(config => {
    const byBuild = new Map();
    metricKeys.forEach(key => pointsOf(config, key).forEach(p => {
      const build = String(p.build_number);
      if (!byBuild.has(build)) byBuild.set(build, { config: config, build: build, point: p, values: {} });
      byBuild.get(build).values[key] = p.value;
    }));
    rows.push(...byBuild.values());
  });
  return rows;
}

// Sorted by get(row), numbers as numbers; blanks last in either direction.
function sortRows(rows, get, descending) {
  const blank = v => v == null || v === '' || (typeof v === 'number' && !isFinite(v));
  return rows.slice().sort((a, b) => {
    const x = get(a), y = get(b);
    if (blank(x) || blank(y)) return blank(x) - blank(y);
    const order = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y));
    return descending ? -order : order;
  });
}

// CSV from rows of cells. A text cell a spreadsheet would run as a formula
// (=, +, -, @) is prefixed with ', since model names come from upstream.
function toCsv(rows) {
  const cell = v => {
    if (v == null) return '';
    let text = String(v);
    if (typeof v === 'string' && /^[=+\-@\t\r]/.test(text)) text = "'" + text;
    return /[",\r\n]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  };
  return rows.map(row => row.map(cell).join(',')).join('\n') + '\n';
}

// The points no other point beats on both x and y (higher is better on
// both), in x order: the best tradeoff available at each x.
function paretoFrontier(points) {
  const frontier = [];
  let bestY = -Infinity;
  points.slice().sort((a, b) => b.x - a.x || b.y - a.y).forEach(p => {
    if (p.y > bestY) { frontier.push(p); bestY = p.y; }
  });
  return frontier.reverse();
}

// The shared chart x-axis. Both ends sit at noon, where nightlies are drawn,
// so each point lands on its day's tick. The full window starts the day
// before the cutoff: a nightly finished inside it can be named for the day
// before. A narrowed window counts back from anchor, the newest nightly (one
// ending today would often be empty); it still ends today, so a stalled
// nightly shows as a gap.
function chartBoundsFor(now, windowDays, days, anchor) {
  const end = dayX(fmtDate(now)),
    start = dayX(fmtDate(now - windowDays * DAY_MS)) - DAY_MS;
  if (days >= windowDays) return { min: start, max: end };
  return { min: Math.max((anchor || end) - days * DAY_MS, start), max: end };
}

const api = {
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
  reportedTonight,
  comparedOvernight,
  REG_TIERS,
  regTier,
  configKeyOf,
  lenLabel,
  shapeLabel,
  variantLabel,
  compact,
  accuracyMetricLabel,
  accuracyPercent,
  accuracyPoints,
  DAY_MS,
  rankNightlies,
  chartBoundsFor,
  runDay,
  runRows,
  sortRows,
  toCsv,
  paretoFrontier,
  nightlyStatus,
};
if (typeof module === 'object' && module.exports) module.exports = api;
else root.PerfAnalysis = api;
})(this);
