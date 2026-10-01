# How regressions are detected

The rule the page uses to call a change a regression or an improvement, and
the reasons behind each part of it. For anyone who needs to trust, question or
change a regression count.

## The rule

**The newest nightly against the run before it.**

| | Counts as a change at |
|---|---|
| Performance metrics | **0.5%** or more |
| Accuracy scores | **1 point** or more (0.01 on the 0–1 scale) |

- Smaller moves are neutral. Wherever the page reports a count, it also says
  how many configs had only smaller moves.
- **Counts are of configs**, not config-metric pairs: one slowdown moves
  several metrics at once. A config counts once however many metrics dropped.
- **Derived metrics are never counted.** Input throughput is total minus
  output, and interactivity is 1 / TPOT, so counting them would count one move
  twice. Their charts still mark the drop. The payload flags each metric's
  `counted`.
- Tests pin both thresholds.
- The payload carries them in `thresholds`, so the page never hard-codes them
  (see the [payload contract](data-pipeline.md#the-published-payload-perf_evaljson)).

## Only overnight changes count

The overnight signals only count configs that reported in the newest nightly
**and** the one before it.

- A config that **skipped tonight** still has two earlier points, but that
  change happened on earlier nights. Counting it would pass an old change off
  as tonight's.
- A config that **ran tonight after skipping** is compared against whatever
  older run it has, so its change spans every night it missed. A config back
  from a two-week outage would otherwise report two weeks of drift as one
  night's regression.

Neither is counted:

- Configs that did not report show up in Coverage instead.
- A returning config is compared normally from its second consecutive night.
- When nothing could be compared at all, the regression panel says *Nothing
  to compare* rather than showing a green *No regressions*.

Every regression signal on the page, perf and accuracy, goes through one
predicate, `comparedOvernight` in `site/js/analysis.js`, which
`tests/js/analysis.test.js` covers. An accuracy task that skipped the
previous nightly shows *skipped #N* instead of a verdict.

## Why these thresholds

**0.5% is chosen, not measured.** With no threshold, about half the
regressions flagged on a typical night were under 0.5%, the smallest 0.015%.
Once `repetitions: 3` lands on the AMD recipes, the spread across those
repetitions is a real noise floor, and the threshold should be derived from
it.

**Accuracy is not reproducible night to night**, despite the fixed dataset:
every AMD workload's gsm8k score moves every night. One gsm8k question out of
1,319 is worth 0.08 points, and the run-to-run spread is about 0.7 points, so
the 1-point threshold sits just above it.

**Failed requests are not known.** A run where some requests failed is not
comparable to a clean one, but perf-eval does not send failed counts to
Databricks, so such a run cannot be told apart.

## No smoothing, on purpose

Reducing measurement noise is the benchmark's job. perf-eval's
`lib/aggregate_perf.py` repeats a benchmark `repetitions` times on the same
warm server and median-aggregates every field before ingestion. Averaging
again here would blur the night-to-night change this dashboard exists to show.
If a metric is too noisy, the fix is a higher `repetitions` in the recipe.

> [!WARNING]
> **Known upstream gap.** Every NVIDIA workload in `vllm-project/perf-eval`
> sets `repetitions: 3`. None of the eleven AMD (`mi300x`/`mi355x`) workloads
> set it, so they default to `1`: every point on this dashboard is a single,
> unaggregated run. Raising `repetitions` on those recipes is the right way to
> make night-to-night comparison trustworthy.

## Time window

- The view shows a trailing **30 days**, anchored to *now*. If the nightly
  stops reporting, the page says so rather than showing old numbers as current
  (see [When the data is stale](reading-the-dashboard.md#when-the-data-is-stale)).
- The window can be narrowed, never widened. It comes from
  `display_window_days` in the payload, which `aggregate.py` owns.
- Detection runs in the page, not in `aggregate.py`, because the window
  decides which run is "latest" and which is its predecessor. The payload
  publishes each series and the thresholds; it does not precompute a verdict.
