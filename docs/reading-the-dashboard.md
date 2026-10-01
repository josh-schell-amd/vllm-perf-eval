# Reading the dashboard

What each card, tab and colour on the page means. For anyone using the
dashboard, whether or not they touch the code. For the rules behind the
regression counts, see [regression-detection.md](regression-detection.md).

## KPI cards

Each card answers "is tonight's build healthy?" Every card except *Latest
nightly* follows the filters.

| Card | Shows |
|---|---|
| Latest nightly with AMD results | Its build, date and vLLM commit, linking to the Buildkite build when one is known (see below). There is no gap or "still running" detail: the collector does not poll Buildkite's build state, so a nightly that failed before producing any result is invisible, and one still in progress shows only what has reported so far |
| Performance overnight | Median Output Throughput change vs the previous nightly. Green for any gain, red for a drop of at least 0.5% |
| Regressions overnight | Configs with at least one metric 0.5% or more worse. Green at zero, when something was compared |
| Improvements overnight | Configs with at least one metric 0.5% or more better. A config can be in both |
| Accuracy overnight | Models whose accuracy dropped at least 1 point. Green at zero, when accuracy reported |
| Coverage | What reported, against what the recipes expect |

> [!IMPORTANT]
> On *Performance overnight*, colour follows the median, not the regression
> rule. It is not a regression alarm.

All filtering is in the header: a dropdown per facet (device, model,
precision, ISL/OSL, concurrency), then two toggles with the number of
configs each keeps. **Regressed** keeps configs that regressed overnight.
**Failed requests** keeps configs whose newest run had failed requests; those
counts come from the benchmark itself (`vllm bench serve` reports completed
and failed requests), and a run with failures is not comparable to a clean
one.

On the charts, a run with failed requests is an orange ▲ (trends and
history) or a ⚠ before a bar's value (Performance), with a key above. vLLM
computes latency and throughput from the completed requests only, so such a
run can look like an improvement when it is not. The regression panel follows
the header toggles, as the charts do.

A point links to its Buildkite build page (not a specific job: there is no
job ID) when the build is known. Perf rows carry no build of their own, so a
perf point gets the build whose accuracy results ran the same vLLM commit.
Without one, the build is shown as its day (`#2026-10-01`), with no link.

## When the data is stale

The view shows a trailing 30 days, anchored to *now*, so old numbers are
never presented as current. There is no longer a day-by-day account of
missing nightlies (`no nightly ran`, `#603 failed`, `#606 still running`,
…) — that relied on polling Buildkite's build state, which the collector no
longer does, and guessing at gaps with no signal behind them would just be
noise (see `docs/data-pipeline.md`'s Data identity section). The one
remaining staleness signal is generic and always accurate:

| The notice says | Meaning |
|---|---|
| `the Collect and Deploy workflow may not be running` | The payload was last generated more than 36 hours ago |

With no results at all in 30 days, the dashboard goes empty and says so.

The footer's **Download the data (JSON)** link saves the exact payload the page
is showing.

## Tabs

Filters, the tab, the picked metric and the chart window are all kept in the
URL, so **Copy link** reproduces the view.

### Performance (default)

- Each metric button shows which way is better: ▲ higher, ▼ lower, spelled
  out beside the chart heading. (In the regression list and table, ▲▼ mark
  which way a value moved instead.)

- One bar chart per model and device, since per-GPU numbers don't compare
  across devices.
- Darker bar = higher concurrency. Red outline = at least 0.5% worse than
  the previous run. Faded = not in the newest nightly: the bar shows its last
  value from an older build. A card with any such bar says so at its top
  right, in orange: `4 of 4 not in #625`.
- Hover for the value, build, commit and change: `vs the previous run
  (2026-09-29)`, or `vs an older run (2026-09-18)` if the config skipped the
  previous run, or *No previous run*. Changes are counted by run, not by
  night: nightlies can skip days, so the previous run need not be last
  night's. The build is the Buildkite build number where known, else the
  day.
- Click a bar for its history.
- The detail table below lists each config's newest values, each with its own
  change against the previous run beneath it, so no metric needs picking
  first. The metric picked above joins as a highlighted column when it is not
  one of the headline five. A row expands to its run details. Throughput bars and latency shading are scaled within that
  row's model and device, since per-GPU numbers differ by orders of magnitude
  across models.

### Trends

- Like Performance: pick a metric, then one chart per model and device, with
  a line per configuration named in its legend. The chosen metric is shared
  with the Performance tab.
- Red line = regressed on that metric, newest point ringed, and its legend
  entry says so in red: `1K/1K c=1 · regressed ▼1.3%`. A card with a
  regression has a red border. Click a point for that config's history.
- The x-axis labels every day, none skipped, as ATOM does, from that chart's
  own points: a day with a point shows the short vLLM commit it measured
  (`9/25 abc1234`, the date in the text colour, the commit in blue). A day without one reads `9/24 missing` in orange: no
  nightly ran, or it ran without these configs (or has not reported them
  yet: there is no "still running" signal). Days before the chart's first
  point, or whose nightly is not due yet, show the date alone. The history
  chart uses the same axis.
- The chart window (1–30 days) changes the charts only, never the regression
  counts.

### Throughput vs Latency

- Interactivity (1 / TPOT) against Total Throughput, one curve per shape
  across concurrency, per model and device.
- A concurrency scaling chart, and a throughput heatmap by ISL/OSL ×
  concurrency.
- Red ring = throughput regression overnight.

### Accuracy

- lm-eval score per model, device and task, against the previous nightly.
- Flexible-extract is preferred over strict-match, which also grades format
  (a model can score far lower on strict-match only because of how it formats answers).

### Data

- Every nightly of every configuration in the window, one row each: date,
  build, config, the headline metrics, failed requests and vLLM commit. The
  newest value per config, with every metric's change, is the Performance
  table's expanded row.
- Click a heading to sort (again to reverse); the sort is kept in the URL.
  Click a row for that config's history.
- **Copy CSV** copies exactly the rows shown, in that order, with the full
  model name, vLLM commit and build URL. Text a spreadsheet would run as a
  formula is prefixed with `'`.
### Missing panel (bottom of the page)

- Configs the recipes expect but the newest build with results didn't
  report, grouped by workload. A nightly still in progress counts its
  unfinished workloads here until they report.
- It matters because a workload that OOMs just stops reporting: it vanishes
  from the averages instead of showing as a regression.

## Colour

- **Red = regression, and nothing else.** The palette has no red or pink.
- **Per metric:** a config turns red only on the charts where it regressed.
- **By size:** regression chips, row edges and change arrows share one scale:
  yellow up to 2.5%, orange up to 5%, red above 5%. A change that is not
  overnight (the config skipped the previous run) stays grey.
- **Green = improvement.**

> [!NOTE]
> Adding a colour? Add it to both `PALETTE` and `PALETTE_LIGHT`, in the same
> position, and keep it out of the 0–20° hue range.

## Our throughput numbers are not ATOM's

They are the same measurements, normalized differently. Check this before
comparing the two dashboards number for number.

| ATOM | Here | Relationship |
|---|---|---|
| Total Throughput (tok/s) | `tput_per_gpu`, "Total Throughput" | ours = ATOM ÷ GPUs |
| Output Throughput (tok/s) | `output_tput_per_gpu`, "Output Throughput" | ours = ATOM ÷ GPUs |
| Interac., `1000 / TPOT` (tok/s/user) | `mean_intvty`, "Interactivity" | identical |
| TTFT, TPOT (ms) | `mean_ttft`, `mean_tpot` (stored in s) | identical |

- perf-eval divides once at ingest, by its `tp`: the recipe's
  `metadata.tp`, else TP × DP. Every value here is already per-GPU, which is
  what makes two devices comparable. (A recipe using PP or PCP would be
  divided by fewer GPUs than it uses; none does yet.)
- ATOM reports what the harness emitted, and divides by GPU count only in its
  tradeoff charts.
- The `/GPU` lives in the **unit**, not the label. Axis titles and tooltips
  read both out of `metric_meta`, so they cannot go stale when a metric is
  renamed.
