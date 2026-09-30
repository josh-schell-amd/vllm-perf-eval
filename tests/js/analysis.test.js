// Tests for the page's pure logic, site/js/analysis.js. Node's built-in
// runner, no dependencies: node --test tests/js/*.test.js
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const a = require('../../site/js/analysis.js');

const DAY = a.DAY_MS;
const HIGHER = { better: 'higher' };
const LOWER = { better: 'lower' };
const point = night => ({ night: night });

test('esc neutralises every character that can break out of markup', () => {
  assert.equal(a.esc(`<a href="x" onclick='y'>&</a>`),
    '&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;&lt;/a&gt;');
  assert.equal(a.esc(null), '');
});

test('safeUrl passes only http(s) links', () => {
  assert.equal(a.safeUrl('https://buildkite.com/x'), 'https://buildkite.com/x');
  assert.equal(a.safeUrl('javascript:alert(1)'), '');
  assert.equal(a.safeUrl(' data:text/html,x'), '');
  assert.equal(a.safeUrl(undefined), '');
});

test('a nightly shows the date Buildkite names it by, not the day it finished', () => {
  assert.equal(a.nightlyDay({ nightly_date: '2026-09-25', date: '2026-09-26T14:00:28Z' }), '2026-09-25');
});

test('a nightly with no Buildkite date falls back to the day it finished', () => {
  assert.equal(a.nightlyDay({ nightly_date: '', date: '2026-09-26T14:00:28Z' }), '2026-09-26');
  assert.equal(a.nightlyDay({ nightly_date: 'Sept 25', date: '2026-09-26T14:00:28Z' }), '2026-09-26');
});

test('dayX places a day at noon UTC', () => {
  assert.equal(new Date(a.dayX('2026-09-25')).toISOString(), '2026-09-25T12:00:00.000Z');
  assert.equal(a.dayX('not a day'), 0);
});

test('a drop past the threshold is a regression', () => {
  const v = a.verdict([100, 99], HIGHER, 0.005);
  assert.equal(v.regressed, true);
  assert.equal(v.improved, false);
  assert.ok(Math.abs(v.ratio - -0.01) < 1e-12);
});

test('a drop inside the threshold is not a regression', () => {
  assert.equal(a.verdict([100, 99.6], HIGHER, 0.005).regressed, false);
});

test('for a lower-is-better metric, a rise is the regression', () => {
  assert.equal(a.verdict([10, 11], LOWER, 0.005).regressed, true);
  assert.equal(a.verdict([10, 9], LOWER, 0.005).improved, true);
});

test('an improvement is never a regression, however large', () => {
  assert.equal(a.verdict([100, 200], HIGHER, 0.005).regressed, false);
});

test('a flat value stays neutral even with a zero threshold', () => {
  assert.equal(a.verdict([100, 100], HIGHER, 0).regressed, false);
});

test('no verdict without two values or with a zero base', () => {
  assert.equal(a.verdict([100], HIGHER, 0.005), null);
  assert.equal(a.verdict([0, 5], HIGHER, 0.005), null);
});

test('an accuracy move of exactly the threshold counts, despite float error', () => {
  // 0.9334 - 0.9234 is not exactly 0.01 in floating point.
  const v = a.accuracyVerdict([0.9334, 0.9234], 0.01);
  assert.equal(v.counted, true);
  assert.equal(v.regressed, true);
});

test('an accuracy gain counts but is not a regression', () => {
  const v = a.accuracyVerdict([0.90, 0.92], 0.01);
  assert.equal(v.counted, true);
  assert.equal(v.regressed, false);
});

test('a config is compared overnight only against the previous nightly', () => {
  const pts = [point('n23'), point('n25')];
  assert.equal(a.comparedOvernight(pts, 'n25', 'n23'), true);
  // It skipped n24, the previous nightly: a two-night change.
  assert.equal(a.comparedOvernight(pts, 'n25', 'n24'), false);
  // Not in tonight's nightly at all.
  assert.equal(a.comparedOvernight(pts, 'n26', 'n25'), false);
  assert.equal(a.comparedOvernight([point('n25')], 'n25', 'n23'), false);
  assert.equal(a.reportedTonight([], 'n25'), false);
});

test('a rebuild of a nightly is the same nightly; a quiet day on one commit is not', () => {
  const b605 = { nightly_date: '2026-09-28', vllm_commit: 'abc' };
  assert.equal(a.nightKey(b605, '605'), a.nightKey({ ...b605 }, '607'));
  assert.notEqual(a.nightKey(b605, '605'), a.nightKey({ ...b605, nightly_date: '2026-09-27' }, '603'));
  // Mirrors store.nightly_identity's fallback.
  assert.equal(a.nightKey({}, '42'), 'build:42');
});

test('nightlies rank newest day first, then newest build for a shared day', () => {
  const nights = new Map([
    ['n23', { x: a.dayX('2026-09-23'), builds: ['599'] }],
    ['n25a', { x: a.dayX('2026-09-25'), builds: ['601'] }],
    ['n25b', { x: a.dayX('2026-09-25'), builds: ['600', '602'] }],
  ]);
  assert.deepEqual(a.rankNightlies(nights), ['n25b', 'n25a', 'n23']);
});

test('the full chart window runs noon to noon, from the day before the cutoff', () => {
  const now = Date.parse('2026-09-28T16:33:00Z');
  const b = a.chartBoundsFor(now, 14, 14, 0);
  assert.equal(new Date(b.max).toISOString(), '2026-09-28T12:00:00.000Z');
  // Cutoff 2026-09-14T16:33; a nightly finished after it can be named 09-13.
  assert.equal(new Date(b.min).toISOString(), '2026-09-13T12:00:00.000Z');
});

test('a narrowed chart window counts back from the newest nightly', () => {
  const now = Date.parse('2026-09-28T16:33:00Z');
  const anchor = a.dayX('2026-09-25');
  const b = a.chartBoundsFor(now, 14, 3, anchor);
  assert.equal(b.min, anchor - 3 * DAY);
  assert.equal(new Date(b.max).toISOString(), '2026-09-28T12:00:00.000Z');
});

test('a narrowed chart window never reaches past the full one', () => {
  const now = Date.parse('2026-09-28T16:33:00Z');
  const b = a.chartBoundsFor(now, 14, 13, a.dayX('2026-09-14'));
  assert.equal(b.min, a.chartBoundsFor(now, 14, 14, 0).min);
});

test('regression tiers follow the displayed one-decimal value', () => {
  const tier = m => a.REG_TIERS[a.regTier(m)].cls;
  assert.equal(tier(0.025), 'minor');
  assert.equal(tier(0.0251), 'minor'); // shows as 2.5%
  assert.equal(tier(0.026), 'moderate');
  assert.equal(tier(0.05), 'moderate');
  assert.equal(tier(0.07), 'major');
});

test('an unnamed precision is left out of a config variant', () => {
  assert.equal(a.variantLabel({ precision: '', parallel_label: 'TP2' }), 'TP2');
  assert.equal(a.variantLabel({ precision: 'fp8', parallel_label: 'TP8 · EP' }), 'fp8 TP8 · EP');
});

test('lengths shorten to K only on whole multiples of 1024', () => {
  assert.equal(a.lenLabel(8192), '8K');
  assert.equal(a.lenLabel(1000), '1000');
  assert.equal(a.lenLabel(null), '?');
});

test('accuracy changes read in points, signed on request', () => {
  assert.equal(a.accuracyPoints(0.015, true), '+1.5 pt');
  assert.equal(a.accuracyPoints(-0.003, true), '-0.3 pt');
  assert.equal(a.accuracyPercent(0.9575), '95.75%');
});

// nightly_runs, newest first, as the payload publishes them.
const run = (build, day, results, state = 'failed') =>
  ({ build: String(build), nightly_date: day, state: state, amd_results: results });
const collectedAt = Date.parse('2026-09-28T17:17:00Z');
const latest601 = { build: '601', day: '2026-09-25' };
const gapsOf = s => s.gaps.map(g => [g.day, g.runs.map(r => r.build)]);

test('each later day is a gap: nightlies with no AMD results, or none at all', () => {
  const runs = [run(602, '2026-09-26', 0), run(601, '2026-09-25', 52)];
  const s = a.nightlyStatus(runs, latest601, collectedAt);
  assert.equal(s.known, true);
  // 09-27 was due by 16:00 UTC on 09-28 and nothing ran; 09-28 is not due yet.
  assert.deepEqual(gapsOf(s), [['2026-09-27', []], ['2026-09-26', ['602']]]);
});

test('a nightly still inside its usual finishing time is not a gap', () => {
  const runs = [run(601, '2026-09-25', 52)];
  assert.deepEqual(a.nightlyStatus(runs, latest601, Date.parse('2026-09-26T15:00:00Z')).gaps, []);
});

test('an empty nightly that finished early is a gap before it is due', () => {
  const runs = [run(603, '2026-09-26', 0), run(601, '2026-09-25', 52)];
  const early = Date.parse('2026-09-27T10:00:00Z');
  assert.deepEqual(gapsOf(a.nightlyStatus(runs, latest601, early)), [['2026-09-26', ['603']]]);
});

test('an up-to-date dashboard has no gaps', () => {
  const runs = [run(605, '2026-09-27', 50), run(601, '2026-09-25', 52)];
  assert.deepEqual(a.nightlyStatus(runs, { build: '605', day: '2026-09-27' }, collectedAt).gaps, []);
});

test('with no runs recorded, gaps are still reported, their cause unknown', () => {
  const s = a.nightlyStatus([], latest601, collectedAt);
  assert.equal(s.known, false);
  assert.deepEqual(gapsOf(s), [['2026-09-27', []], ['2026-09-26', []]]);
});

test('a newer empty build on the latest day is a rebuild, not a gap', () => {
  const runs = [run(602, '2026-09-25', 0, 'canceled'), run(601, '2026-09-25', 52)];
  const s = a.nightlyStatus(runs, latest601, Date.parse('2026-09-26T15:00:00Z'));
  assert.deepEqual(gapsOf(s), []);
});

test('each nightly of a config is one row, with its value on every metric', () => {
  const config = {
    metrics: {
      tput: [{ build_number: '599', value: 10 }, { build_number: '601', value: 11 }],
      ttft: [{ build_number: '601', value: 0.2 }],
    },
  };
  const rows = a.runRows([config], ['tput', 'ttft'], (c, key) => c.metrics[key] || []);
  assert.deepEqual(rows.map(r => [r.build, r.values]), [
    ['599', { tput: 10 }],
    ['601', { tput: 11, ttft: 0.2 }],
  ]);
});

test('rows sort numbers as numbers, blanks last in either direction', () => {
  const rows = [{ v: 10 }, { v: null }, { v: 9 }, { v: 100 }];
  assert.deepEqual(a.sortRows(rows, r => r.v, false).map(r => r.v), [9, 10, 100, null]);
  assert.deepEqual(a.sortRows(rows, r => r.v, true).map(r => r.v), [100, 10, 9, null]);
});

test('sorting is stable, so an earlier sort breaks ties', () => {
  const rows = [{ k: 'b', n: 1 }, { k: 'a', n: 1 }];
  assert.deepEqual(a.sortRows(rows, r => r.n, true).map(r => r.k), ['b', 'a']);
});

test('CSV quotes what needs quoting', () => {
  assert.equal(a.toCsv([['a', 'b,c', 'say "hi"', null, 1.5]]), 'a,"b,c","say ""hi""",,1.5\n');
});

test('CSV text a spreadsheet would run as a formula is neutralised', () => {
  assert.equal(a.toCsv([['=HYPERLINK("x")', '@cmd', '+1']]), '"\'=HYPERLINK(""x"")",\'@cmd,\'+1\n');
});

test('CSV leaves negative numbers alone', () => {
  assert.equal(a.toCsv([[-0.5]]), '-0.5\n');
});

test('a nightly still running is not a gap, and says what it waits on', () => {
  const running = { ...run(606, '2026-09-29', 0, 'running'), amd_pending: ['kimi_k2_5_mi300x'] };
  const s = a.nightlyStatus([running, run(605, '2026-09-28', 39)],
    { build: '605', day: '2026-09-28' }, Date.parse('2026-09-30T17:17:00Z'));
  assert.deepEqual(gapsOf(s), []);
  assert.deepEqual(s.running.map(r => r.build), ['606']);
});

test('a failing build is still going too; a failed one is a gap', () => {
  const at = Date.parse('2026-09-30T17:17:00Z');
  const latest = { build: '605', day: '2026-09-28' };
  const failing = a.nightlyStatus([run(606, '2026-09-29', 0, 'failing')], latest, at);
  assert.deepEqual(gapsOf(failing), []);
  const failed = a.nightlyStatus([run(606, '2026-09-29', 0, 'failed')], latest, at);
  assert.deepEqual(gapsOf(failed), [['2026-09-29', ['606']]]);
});

test('only a build still going counts as running', () => {
  assert.equal(a.isOngoing({ state: 'running' }), true);
  assert.equal(a.isOngoing({ state: 'failing' }), true);
  assert.equal(a.isOngoing({ state: 'failed' }), false);
  assert.equal(a.isOngoing({ state: 'passed' }), false);
});
