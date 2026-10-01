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
                databricks_collect.py            (recipes: expected configs,
                           |                       perf precision/parallelism,
                    data/events.jsonl              accuracy model/device)
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
`message`. Only nightly rows are fetched, and from the eval table only
`kind = 'results'` rows (one per lm_eval results file), never the
per-question `samples` rows that make up most of it, and only the fields
read (`EVAL_FIELDS`).

There is no persisted event store. Databricks already retains full history,
so every run queries it fresh (filtered to `WINDOW_DAYS`) and rebuilds
`data/events.jsonl` from scratch; it is a plain file for `aggregate.py` to
read in the same run, not a store anyone pushes to.

Queries go through Databricks' SQL Statement Execution REST API
(`run_query`), with `requests`, not `databricks-sql-connector`: its Thrift
paging failed on our results, and it pulled a dozen packages into the job
that holds the token. Each query is one read-only `SELECT`. The collector
follows every result chunk and fails if fewer rows arrive than the result
manifest reports.

Workload recipes are still read from the public `vllm-project/perf-eval`
repo, for three things the Databricks rows don't carry correctly: the
`expected_configs`/`expected_accuracy` snapshot behind the Coverage card, the
precision and parallelism of perf rows, and a `model`/`device` label for
accuracy rows (see [Data identity](#data-identity)).

Recipes are read at each result's build's perf-eval commit (the
`buildkite_commit` its eval rows carry), so a result is labeled by the
recipe that ran it, and Coverage uses the newest build's commit. `main` is
read only for a result whose build is unknown. This is what the old Buildkite collector did: a config a
later recipe dropped (an old TP2 run, say) keeps its real label, and is not
counted missing from a build whose recipe no longer had it. One archive
download per distinct commit.

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
| Databricks cannot be reached, the query fails, or fewer rows arrive than it reported | Fails the run: no payload is built from a partial or absent query |
| A row is dropped for being out of scope or unusable | Counted by reason in the run log (counts only), so a missing chart can be traced |
| A row's `message` is not a JSON object | Skipped |
| A row is missing fields a perf/accuracy event needs | Skipped, and counted |
| A perf row with no positive throughput metrics | Skipped, rather than published as zero throughput |
| A perf row with no matching recipe | Kept, with its own TP and an unstated precision, and counted |
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
- **The build shown is the real Buildkite build where it can be found.**
  Perf rows carry no build, but eval rows do, and a nightly's perf and eval
  jobs share one image. `buildkite_builds` maps each vLLM commit to the one
  build whose eval rows ran it, and every result with that commit takes its
  build number, URL, commit and branch. A commit with no eval row, or one
  that two builds ran, keeps the day as its build (`#2026-10-01`). The run log
  counts how many results were named each way.
- **There is no "nightly failed" or "still running" visibility.** The old
  collector polled Buildkite's builds API to know about a nightly that ran
  but produced no AMD results, or is still going. Nothing here does that
  anymore: a nightly is invisible until it has actually produced at least one
  ingestable row. Accepted tradeoff — see the KPI card's behavior in
  [reading-the-dashboard.md](reading-the-dashboard.md).
- **A perf row's precision and parallelism come from its recipe, not the
  row.** perf-eval stamps `precision` from the recipe's
  `metadata.precision`, else a marker in the model name, else `bf16`
  (`precision_from_model` in its `lib/parse_workload.py`). So an int4 or fp8
  checkpoint whose name says neither is stored as `bf16`. Its `tp` is
  `metadata.tp`, else TP×DP as one number. `recipe_labels` keys every recipe
  config on what a row does carry: model, device, ISL/OSL, concurrency and
  that same `tp` (`bench_tp` in `recipes.py`). Each matching row takes the
  recipe's precision (derived as for Coverage: recipe, `--quantization`, the
  checkpoint's `config.json`, `--dtype`) and full parallelism map, so results
  and Coverage agree. A row with no match, or one matching two recipes that
  label it differently, keeps its own TP and an unstated precision, and the
  run log counts it.
  - **Per-GPU metrics are used as the row has them**, already divided by
    that `tp`. The raw throughput isn't stored, so a recipe using pipeline or
    prefill-context parallelism can't be re-divided by `gpu_count()`. This
    only matters once a recipe uses PP or PCP.
- **Accuracy rows have no `model`/`device` field at all** — `lib/ingest.py`
  never stamps them. Both are recovered by joining the row's `workload` name
  against the recipes at the row's own `buildkite_commit`
  (`databricks_collect.py:accuracy_event`), so a workload later renamed or
  removed still labels its older rows.
- **Accuracy rows do carry real Buildkite identity** (`buildkite_build_number`,
  `buildkite_build_url`, `buildkite_branch`, `buildkite_commit`, `vllm_commit`)
  because `lib/ingest.py` stamps it directly. That is what names the build of
  every result with the same commit, perf included (above). It is not used
  for grouping: perf and accuracy results from the same day and commit merge
  into one nightly via the day-bucket key.
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
- **`builds` is keyed by Buildkite build number** (`"617"`) where
  `buildkite_builds` found one for the commit, and by calendar day
  (`"2026-09-30"`) otherwise. `build_url` is the Buildkite build page (not a
  per-job deep link: there is no job ID) when the number is real, and `""`
  for a day key. `nightly_runs` is always empty: nothing reports a nightly
  that produced no results (see [Data identity](#data-identity)).

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
  // Each build a point names, once: series points carry only its key. A
  // Buildkite build number where known, else the day.
  "builds": {
    "617": { "date": "2026-09-30 17:44:42", "nightly_date": "2026-09-30", "vllm_commit": "...",
             "build_commit": "...", "image": "...",
             "build_url": "https://buildkite.com/vllm/perf-eval/builds/617" },
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
          "series": [{ "build": "617", "value": 1200.0,
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
