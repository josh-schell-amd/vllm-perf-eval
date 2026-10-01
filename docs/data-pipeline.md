# How the data flows

How results get from Databricks to the page: where they are stored, when
they are collected, how failures are handled, how results are identified,
and the payload contract between the collector and the page. For anyone
changing the collector or the payload, or debugging a missing or wrong
result.

## Overview

```
vllm_perf_data_ingest        vllm_eval_data_ingest      GitHub vllm-project/perf-eval
  (Databricks, full history)  (Databricks, full history)  workloads/*.yaml at main
            |                            |                            |
            +--------------+-------------+--------------+-------------+
                           |                             |
                databricks_collect.py            (recipe: expected configs,
                           |                       and model/device for
                    data/events.jsonl              accuracy rows)
                     (this run only)
                           |
                     aggregate.py
                           |
                  data/perf_eval.json
                           |
                build_site.py + seal.py        sealed with the dashboard login
                           |
     site/ + perf_eval.sealed.json -> _site/   -> GitHub Pages
```

The two Databricks tables are Zerobus event logs the sibling `perf-eval`
repo's ingest scripts write to directly (`lib/ingest_perf.py` for
`vllm_perf_data_ingest`, `lib/ingest.py` for `vllm_eval_data_ingest`); nothing
here ever touches Buildkite. Each row has two VARIANT columns, `message` (the
JSON body the ingest script POSTed) and `request_metadata` (HTTP request
info, including an ingest `timestamp`); every field of interest lives inside
`message`.

There is no persisted event store. Databricks already retains full history,
so every run queries it fresh (filtered to `WINDOW_DAYS`) and rebuilds
`data/events.jsonl` from scratch; it is a plain file for `aggregate.py` to
read in the same run, not a store anyone pushes to.

Workload recipes are still read from the public `vllm-project/perf-eval`
repo, but only for two things neither Databricks table can carry itself: the
`expected_configs`/`expected_accuracy` snapshot behind the Coverage card, and
a `model`/`device` label for accuracy rows (`vllm_eval_data_ingest` carries
neither; see [Data identity](#data-identity)). Recipes are always read at
`main` — there is no per-nightly perf-eval commit to pin to anymore, so a
recipe rename or removal can change how an older result is labeled. Accepted
tradeoff, in keeping with the rest of this design: see below.

## Where the data lives

| Where | Holds |
|---|---|
| Databricks, `vllm_perf_data_ingest` / `vllm_eval_data_ingest` | Every result ever ingested, owned by the `perf-eval` pipeline, not this repo |
| This repository, `main` (public) | Source only: no results, ever |
| GitHub Pages | The site and `perf_eval.sealed.json`: the payload, encrypted with the dashboard login |

The plain payload exists only inside a run. The page decrypts the sealed copy
after sign-in ([deploying.md](deploying.md#the-login)).

## Collection schedule

- `collect-and-deploy.yml` collects **hourly at :17, 14:00-21:00 UTC**:
  9am-3pm US Central in both daylight and standard time (cron is UTC and
  ignores daylight saving). Also on manual dispatch.
- A push that touches `site/`, `scripts/build_site.py`, or the modules the
  payload is built from (`aggregate.py`, `normalize.py`, `events.py`,
  `recipes.py`, `databricks_collect.py`) still re-collects from Databricks:
  there is no local store to rebuild from without querying it again, and a
  query is cheap next to the old Buildkite artifact scan.
- A nightly is visible as soon as its results are ingested into Databricks —
  there is no separate "has the build finished" check, because there is no
  Buildkite build polling at all anymore (see [Data identity](#data-identity)
  on what that costs).

## Failure handling

| Situation | Result |
|---|---|
| Databricks cannot be reached, or the query fails | Fails the run: no payload is built from a partial or absent query |
| A row's `message` is not valid JSON, or is missing fields a perf/accuracy event needs | Skipped |
| A perf row with no positive throughput metrics | Skipped, rather than published as zero throughput |
| `data/events.jsonl` (this run's file) cannot be read by `aggregate.py` | Fails the run |
| The live payload cannot be fetched or opened with the login | Counts as changed, so the site is deployed |
| The payload fails `aggregate.py`'s sanity checks | Not written, so nothing malformed is persisted or deployed |

## Redundant deploys are skipped

`aggregate.py` restamps `generated_at` on every run, so the payload always
differs byte for byte even when no new nightly arrived, and Pages allows only
about ten builds an hour. `payload_changed.py` compares the fresh payload with
the one **live on the site**, ignoring `generated_at`, and the deploy is
gated on the result.

- It compares against the live copy, not any locally stored copy, so a failed
  deploy cannot count as published.
- The skip applies **only to scheduled runs**. A push to `site/` or a manual
  dispatch always republishes.
- A missing or unreadable previous payload counts as changed, so the failure
  mode is one redundant deploy, not a silently unpublished update.

## Retention

One number, `WINDOW_DAYS = 30` in `scripts/perf_eval/__init__.py`. Unlike the
old Buildkite-artifact collector, this is only a **query window**, not a
retention limit — Databricks keeps full history regardless, so raising this
number shows more nightlies without losing anything.

## Data identity

This section changed the most in the move away from Buildkite. Read it if a
nightly is missing, split into two, or mislabeled.

- **A nightly's identity is the calendar-day bucket of its ingest timestamp**
  (`YYYY-MM-DD`, from the row's own `date` for perf, `request_metadata`'s
  `timestamp` for accuracy — both already UTC), not a Buildkite build number
  or its message-parsed nightly date. This is simpler but coarser: **a
  nightly retried on a different calendar day is not folded back into the
  original one**, it just becomes its own day's entry. `vllm_commit`, parsed
  out of the image tag for perf rows (real, straight off the row, for
  accuracy rows), still folds two rows from the same day and commit into one
  nightly if collected separately.
- **There is no "nightly failed" or "still running" visibility.** The old
  collector polled Buildkite's builds API to know about a nightly that ran
  but produced no AMD results, or is still going. Nothing here does that
  anymore: a nightly is invisible until it has actually produced at least one
  ingestable row. Accepted tradeoff — see the KPI card's behavior in
  [reading-the-dashboard.md](reading-the-dashboard.md).
- **Precision, parallelism and per-GPU metrics are trusted directly from
  Databricks**, not re-derived from the recipe. `vllm_perf_data_ingest` rows
  already carry a resolved `precision` string and pre-converted per-GPU
  throughput/latency metrics (see `lib/ingest_perf.py` in the sibling
  `perf-eval` repo). The `parallelism` dict is built from the row's own
  `tp`/`ep`/`dp_attention` scalars (`databricks_collect.py:perf_parallelism`),
  not parsed from `serve_args`.
  - **Known gap:** `tp` there means TP×DP as a single int, not vLLM's
    individual flags, so a recipe using pipeline or prefill-context
    parallelism cannot be distinguished from one that doesn't, and the
    per-GPU divisor the row already applied may not match what
    `gpu_count()` would compute from a full parallelism map. The raw,
    un-divided throughput isn't stored anywhere to re-derive it. Only
    matters once a recipe actually uses PP or PCP.
- **Accuracy rows have no `model`/`device` field at all** — `lib/ingest.py`
  never stamps them. Both are recovered by joining the row's `workload` name
  against the recipe map fetched at `main`
  (`databricks_collect.py:accuracy_event`). A workload later renamed or
  removed from `main` means older rows under the old name can't be labeled
  anymore (same "recipe drifted from what actually ran" tradeoff as the
  day-bucket identity above), but the AMD/nightly scope check still passes
  via the workload stem or image, so the row isn't silently dropped, only
  unlabeled.
- **Accuracy rows do carry real Buildkite identity** (`buildkite_build_number`,
  `buildkite_build_url`, `buildkite_branch`, `buildkite_commit`, `vllm_commit`)
  because `lib/ingest.py` stamps it directly — used for display (`build_url`
  in the payload) but not for grouping, so perf and accuracy results from the
  same day still merge into one nightly via the day-bucket key.
- **A series is what ran:** model, device, precision, parallelism, ISL/OSL
  and concurrency. Changing any of those starts a new line, and a regression
  is only ever measured within one line.

## The published payload, `perf_eval.json`

Published sealed, as `perf_eval.sealed.json`; this is its content once the
page has opened it. This is the contract between the collector and the page.
The page and its payload always deploy together: change `aggregate.py`, the
page and this section in the same commit.

- `metric_meta` carries `better` (whether higher or lower values are better)
  and `counted` (false for derived metrics, which regression counts skip),
  so the page colours a new metric correctly without a frontend change.
- `thresholds` holds the smallest moves that count, so the page never
  hard-codes them.
- **`builds` is keyed by calendar day** (`"2026-09-30"`), not a Buildkite
  build number. `build_url` is `""` for perf-only nightlies (the table
  carries no link at all) and the real Buildkite build page — not a
  per-job deep link, since there is no per-artifact job ID anymore — for
  nightlies with an accuracy result. `nightly_runs` is always empty: see
  [Data identity](#data-identity).

```jsonc
{
  "generated_at": "2026-01-01T00:00:00Z",
  "scope":      { "hardware": "amd", "runs": "nightly", "description": "..." },
  "pipeline":   { "org": "vllm", "slug": "perf-eval", "url": "..." },
  "metric_meta": { "tput_per_gpu": { "label": "...", "unit": "tok/s/GPU", "better": "higher" } },
  "thresholds": { "perf_rel": 0.005, "accuracy_abs": 0.01 },
  // What the recipes say should run, for Coverage. The page cannot reach
  // GitHub, so the collector snapshots it into the payload.
  "expected": {
    "recorded_at": "2026-01-01T00:00:00Z",
    "configs":  [{ "workload": "wl-mi355x", "run": "8k-in-1k-out-conc-128", "model": "org/Model",
                   "device": "mi355x", "precision": "fp8",
                   "parallelism": { "tensor_parallel_size": 8 }, "parallel_label": "TP8", "gpus": 8,
                   "isl": 8192, "osl": 1024, "conc": 128 }],
    "accuracy": [{ "workload": "wl-mi355x", "model": "org/Model",
                   "device": "mi355x", "task": "gsm8k" }]
  },
  // Each build (day) a point names, once: series points carry only the day.
  "builds": {
    "2026-09-26": { "date": "2026-09-26 14:00:28", "nightly_date": "2026-09-26", "vllm_commit": "...",
                    "build_commit": "", "image": "...", "build_url": "" }
  },
  // Always empty now; kept for schema stability. See Data identity above.
  "nightly_runs": [],
  "models": [{
    "model": "org/Model",
    "perf_configs": [{
      "device": "mi355x", "isl": 8192, "osl": 1024, "conc": 128,
      "parallelism": { "tensor_parallel_size": 8 }, "parallel_label": "TP8", "gpus": 8,
      "label": "8K in / 1K out @ conc 128 (MI355X)",
      "jobs": {},
      "metrics": {
        "tput_per_gpu": {
          "better": "higher", "label": "...", "unit": "tok/s/GPU",
          "series": [{ "build": "2026-09-26", "value": 1200.0,
                       "completed_requests": null, "failed_requests": null }]
        }
      }
    }],
    "accuracy_tasks": [{
      "task": "gsm8k", "metric": "exact_match,strict-match", "primary": true,
      "series": [ /* ... */ ]
    }]
  }],
  "summary":   { "models": 1, "amd_devices": ["mi355x"], "nightlies": 12, "perf_points": 96, "accuracy_points": 12 },
  "retention": { "display_window_days": 30 }
}
```
