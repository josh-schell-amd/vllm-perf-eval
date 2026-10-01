# vLLM Perf Eval

A static dashboard of AMD nightly performance and accuracy results from the
[`vllm/perf-eval`](https://buildkite.com/vllm/perf-eval) Buildkite pipeline.

Live: <https://josh-schell-amd.github.io/vllm-perf-eval/> (sign-in required)

## What it covers

AMD nightlies only: MI-series hardware (`mi300x`, `mi355x`, …), and only
the rows perf-eval marks as part of the nightly schedule (`nightly: true`). NVIDIA (H200, B200, …) results are on
[perf.vllm.ai](https://perf.vllm.ai) instead. Ad-hoc and pull-request builds
are out of scope: a nightly runs the full matrix and so compares with the
last, but an ad-hoc build may cover one workload at one concurrency.

- **KPI cards**: is tonight's build healthy? The latest nightly with AMD
  results, overnight performance, regressions, improvements, accuracy, and
  coverage against what the recipes expect.
- **Tabs**: Performance, Trends, Throughput vs Latency, Accuracy, and Data
  (every run in one sortable, exportable table).
- **A regression** is the newest nightly against the one before it, past a
  threshold (0.5% perf, 1 point accuracy) — never a guess, never smoothed.

Full guide: [Reading the dashboard](docs/reading-the-dashboard.md). Exactly
what counts as a regression, and why: [regression detection](docs/regression-detection.md).

## How it works

One page (`site/`: HTML, CSS, plain JS, vendored Chart.js — no framework, no
build step) fetches one payload, collected hourly from Databricks by a
GitHub Actions workflow. The payload holds real results, so it lives behind
a sign-in: this repository is public and holds no data.

```
Databricks (vllm_perf_data_ingest,       --(read-only query)-->  data/events.jsonl  --aggregate-->  perf_eval.json
            vllm_eval_data_ingest)                 ^              (this run only)                          |
                                                   |                                                 seal with login
perf-eval workload recipes (GitHub, main) ---------+                                                       |
  (precision, parallelism, model/device, coverage)                                                GitHub Pages (public)
```

Databricks already retains full history, so every run queries it fresh
rather than maintaining a local store. The workload recipes label what the
Databricks rows don't carry correctly: a perf row's precision (perf-eval
stores `bf16` when the model name has no precision marker) and parallelism,
an accuracy row's model and device, and the configs Coverage expects.
Details: [data pipeline](docs/data-pipeline.md).

## Deploying your own copy

This repository (public, code only). Full walkthrough — secrets, Pages, push
protection, the login: **[docs/deploying.md](docs/deploying.md)**.

## Where to go next

| If you want to | Read |
|---|---|
| Understand every card, tab and colour | [docs/reading-the-dashboard.md](docs/reading-the-dashboard.md) |
| Know exactly what counts as a regression, and why | [docs/regression-detection.md](docs/regression-detection.md) |
| Follow the data: schedule, failures, cost, retention, the payload contract | [docs/data-pipeline.md](docs/data-pipeline.md) |
| Deploy your own copy: secrets, Pages, push protection, the login | [docs/deploying.md](docs/deploying.md) |
| Change the code: layout, local setup, checks, vendoring | [docs/development.md](docs/development.md) |
| Change code or run workflows as an agent, or start a run safely | [AGENTS.md](AGENTS.md) |

## Feedback

Questions, bugs or ideas: Josh Schell, [josh.schell@amd.com](mailto:josh.schell@amd.com).
The dashboard's footer links the same address.

## Licence

MIT. See [LICENSE](LICENSE). Copyright © Advanced Micro Devices, Inc.,
matching [ROCm/ATOM](https://github.com/ROCm/ATOM). Bundled third-party
notices: [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) (Chart.js, also MIT).

## Relationship to `vllm-ci-dashboard`

This dashboard replaces the `Perf Eval` tab in `vllm-ci-dashboard`, where the
same feature lived inside a ~13k-line shared frontend module. The two run in
parallel for now; retiring the old tab is tracked separately.
