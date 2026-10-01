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

test('isRetired: only a config the newest build neither ran nor expected', () => {
  const cfg = { model: 'm', device: 'mi355x', precision: 'fp8', parallel_label: 'TP2', isl: 1024, osl: 1024, conc: 1 };
  const expected = new Set([a.configKeyOf({ ...cfg, parallel_label: 'TP4' })]);
  assert.equal(a.isRetired({ ...cfg, nights: new Set(['old']) }, 'new', expected), true);
  assert.equal(a.isRetired({ ...cfg, nights: new Set(['old', 'new']) }, 'new', expected), false);
  assert.equal(a.isRetired({ ...cfg, parallel_label: 'TP4', nights: new Set(['old']) }, 'new', expected), false);
  assert.equal(a.isRetired({ ...cfg, nights: new Set(['old']) }, 'new', new Set()), false);
});

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

