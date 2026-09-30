# How the data flows

How results get from Buildkite to the page: where they are stored, when they
are collected, what a run costs, how failures are handled, how results are
identified, and the payload contract between the collector and the page. For
anyone changing the collector, the store or the payload, or debugging a
missing or wrong result.

## Overview

```
Buildkite vllm/perf-eval            GitHub vllm-project/perf-eval
  artifact_paths: results/**/*        workloads/*.yaml (device, tp, precision)
            |                                     |
            +------------------+------------------+
                               |
                   collect_artifacts.py          read-only GETs only
                               |
                      data/events.jsonl          private repo, branch: dashboard-state
                               |
                        aggregate.py
                               |
                    data/perf_eval.json
                               |
                  build_site.py + seal.py        sealed with the dashboard login
                               |
       site/ + perf_eval.sealed.json -> _site/   -> GitHub Pages
```

The upstream pipeline uploads its whole `results/` tree as Buildkite
artifacts, so the collector reads raw `bench-*.json` and `results_*.json`
files directly. Nothing has to be pushed to us, and no webhook receiver needs
hosting.

The workload recipes are read from the public `vllm-project/perf-eval` repo,
one archive download per recipe commit.

## Where the data lives

| Where | Holds |
|---|---|
| This repository, `main` (public) | Source only: no results, ever |
| Private repository (`STATE_REPO`), branch `dashboard-state` | `data/events.jsonl`, the event log, which churns on every collection |
| GitHub Pages | The site and `perf_eval.sealed.json`: the payload, encrypted with the dashboard login |

The plain payload exists only inside a run. The page decrypts the sealed copy
after sign-in ([deploying.md](deploying.md#the-login)).

## Collection schedule

- `collect-and-deploy.yml` collects **hourly at :17, 14:00-21:00 UTC**:
  9am-3pm US Central in both daylight and standard time (cron is UTC and
  ignores daylight saving). Also on manual dispatch.
- **A nightly is collected once its AMD jobs are done**, even while NVIDIA
  jobs keep the build running for hours. A build still going is listed
  separately (`running`, or `failing` once a job has failed), and read only
  when every job running an AMD workload has finished; its results are dated
  by the last of those.
- **A nightly still running is shown as running, not missing.** It is
  recorded with the AMD workloads it has left (`amd_pending`), and the page
  says what it is waiting on instead of reporting that no nightly ran.
- There is no build-finished trigger, since that would need a hosted webhook
  endpoint.
- A push that touches `site/`, `scripts/build_site.py`, the modules the
  payload is built from (`aggregate.py`, `normalize.py`, `store.py`,
  `__init__.py`) or the workflow only rebuilds and redeploys the page from
  stored data. It never calls Buildkite.
- Each run scans the last 30 days of finished `main` builds and keeps those
  whose env has `NIGHTLY` set (`1`, `true`, or `yes`). The commit message
  supplies the nightly's date. A nightly that already has results is not
  listed again, except the newest 3: a job retried the next day adds
  artifacts to a build already seen.

### Manual runs

**Run workflow** has two options. Starting a run is human-gated; see
[AGENTS.md](../AGENTS.md#running-the-workflows-human-gated).

| Option | What it does |
|---|---|
| `rebuild` | Re-downloads the last 30 days of results instead of only new ones, and replaces the store with them. Use it after changing how results are labeled (precision, parallelism, per-GPU division), or to pick up artifacts an older collector missed. The store only holds 30 days, so nothing is lost. If the rebuild finds fewer results than are stored, the store is kept and the run fails. |
| `deploy` | Publishes the site when done (on by default). |

- A rebuild reads each build against the recipes it ran. A recipe that
  changed a label partway through the window still splits that model's series
  until the older results age out.
- A rebuild costs about 1 listing plus 3 per nightly, and one download per
  artifact. `--dry-run --rebuild` shows the exact cost first.
- Locally, `--days` and `--recheck-builds` narrow or widen a normal run.

## Failure handling

| Situation | Result |
|---|---|
| A download keeps failing with a timeout, 429 or gateway error | **Fails the run** after 3 attempts, since skipping it would lose that artifact for good |
| A 4xx, or a body that is not JSON | Skipped with a warning, so one broken artifact cannot block every later collection |
| A bench result with no positive `total_token_throughput` | Treated as a failed benchmark and skipped, rather than published as zero throughput |
| The run aborts mid-scan | The previous store is intact: it is written only after the whole scan completes |
| `dashboard-state` is missing or unreachable | Fails the run at checkout |
| The live payload cannot be fetched or opened with the login | Counts as changed, so the site is deployed |
| Another writer pushed the store first | Fails the run: the store is pushed without force, so it never overwrites |
| The payload fails `aggregate.py`'s sanity checks | Not written, so nothing malformed is persisted or deployed |

## What a run costs Buildkite

| | Requests |
|---|---|
| Builds listing | 1 |
| Artifact listing | 3 per nightly inside the re-check window (one per path filter) |
| Download | 1 per artifact not already ingested |

The re-check window defaults to the newest 3 nightlies, because a nightly can
finish with a failed workload that someone retries later, adding artifacts to
the same build. A steady-state run is `1 + 3×3 = 10` listings plus the new
nightly's artifacts.

> [!TIP]
> **See the cost before you spend it.** `--dry-run` does the listings, reports
> exactly what it would download, and stops, with no downloads and no writes:
>
> ```bash
> BUILDKITE_TOKEN=bkua_... python scripts/perf_eval/collect_artifacts.py --dry-run
> ```

### How the cost is bounded

- `--max-requests` (default 3000) aborts the run rather than continuing. An
  unexpected request volume is a bug worth stopping on.
- Artifact listings use narrow path filters (`*results/*/bench-*.json`,
  `*results/*/*/results_*.json` and `*results/*/*/*/results_*.json`), so the
  pipeline's much larger sample and log tree is never enumerated. The deepest
  one exists because lm-eval writes its results into a subdirectory named
  after the model, under the task directory perf-eval gives it.
- Pagination is capped (10 pages for builds, a shared 10-page budget per build
  for artifacts) and raises rather than looping.
- Retries are capped at 3 attempts and only apply to gateway-ish codes (429,
  502, 503, 504, 520, 522, 524). A 500 is not retried, since it is usually
  persistent. A retry waits until Buildkite's rate limit resets
  (`RateLimit-Reset`, else `Retry-After`, at most 70 s), not a fixed backoff.
- **The rate limit is shared** by everyone using the `vllm` organization's
  API, so the collector pauses for the reset once fewer than 20 requests are
  left, instead of spending the rest. A rebuild takes minutes rather than
  seconds as a result.
- Retry attempts are charged to the budget, so the reported total is what
  Buildkite actually saw.
- The workflow's `concurrency` group prevents two runs overlapping.

## Redundant deploys are skipped

`aggregate.py` restamps `generated_at` on every run, so the payload always
differs byte for byte even when no new nightly arrived, and Pages allows only
about ten builds an hour. `payload_changed.py` compares the fresh payload with
the one **live on the site**, ignoring `generated_at`, and the deploy is
gated on the result.

- It compares against the live copy, not one saved with the event store, so a
  failed deploy cannot count as published.
- The skip applies **only to scheduled runs**. A push to `site/` or a manual
  dispatch always republishes.
- A missing or unreadable previous payload counts as changed, so the failure
  mode is one redundant deploy, not a silently unpublished update.

## Retention

One number, `WINDOW_DAYS = 30` in `scripts/perf_eval/__init__.py`. The page
shows 30 days, so that is all anything keeps:

- `data/events.jsonl` keeps the last 30 days of nightly results, a record of
  each nightly build seen in that time (results or not), the newest recipe
  snapshot, and the IDs of artifacts downloaded in that time.
- `data/perf_eval.json` publishes the last 30 days of nightlies.
- The collector looks back at most 30 days, so it never re-lists a build whose
  results were already dropped.

Both files are written atomically (temp file, then rename). Thirty days of
the log is about a megabyte.

## Data identity

- **A nightly's date is the one Buildkite names it by**, from its message
  ("Nightly run 2026-09-26"), not the day it finished, so the page matches
  what Buildkite shows. A nightly that finishes after midnight UTC would
  otherwise appear a day late. Charts place it at noon on that day. A result
  stored before the date was recorded shows its finish day until a rebuild.
- **Precision is what vLLM loads**, read in this order, first match wins:
  the recipe's own `precision`; `--quantization` in its `serve_args`; the
  checkpoint's `quantization_config` (its `config.json` on Hugging Face,
  fetched once per model per run); `--dtype`; the checkpoint's `dtype`; a
  marker in the model id. So `openai/gpt-oss-120b` reads mxfp4 and
  `moonshotai/Kimi-K2.5` int4 without either recipe saying so. Nothing is
  guessed: "unstated" only when none of these says, e.g. a gated checkpoint
  without `HF_TOKEN`.
- **A series is what ran:** model, device, precision, parallelism, ISL/OSL
  and concurrency. Changing any of those starts a new line, and a regression
  is only ever measured within one line. Renaming a run with the same values
  continues it. A removed config's line stops and ages out, and is not
  reported missing.
- **Parallelism is every `--*parallel*` flag in the recipe's `serve_args`**,
  keyed by vLLM's name with defaults left out, e.g.
  `{"tensor_parallel_size": 4, "enable_expert_parallel": true}`, shown as
  `TP4 · EP`. A flag perf-eval starts using is recorded and separates configs
  without a code change; only a new dimension that multiplies GPUs needs one.
  Events stored before the map existed carry only `tp`, and take the rest from
  the recipe with the same shape.
- **Each build is read against the recipes at the perf-eval commit it ran,**
  never against `main`, so a recipe change landing after a nightly does not
  relabel it.
- **A nightly is identified by the day it is named for and its vLLM
  commit**, falling back to build number. A rebuild of a nightly, even a
  partial one, folds into it: the page treats both builds as one nightly, so
  configs the rebuild skipped still count as tonight's. Two nights that tested
  the same commit stay two nightlies. When two observations share an identity,
  the newer timestamp wins, not the later position in the log.
- **Accuracy takes its model id from the workload recipe**, because lm-eval's
  `config.model` names the client backend (`local-completions`). Standard
  errors and the question count (`sample_len`) are dropped, since they are
  not scores.

## The published payload, `perf_eval.json`

Published sealed, as `perf_eval.sealed.json`; this is its content once the
page has opened it. This is the contract between the collectors and the page. The page and its
payload always deploy together: change `aggregate.py`, the page and this
section in the same commit.

- `metric_meta` carries `better` (whether higher or lower values are better)
  and `counted` (false for derived metrics, which regression counts skip),
  so the page colours a new metric correctly without a frontend change.
- `thresholds` holds the smallest moves that count, so the page never
  hard-codes them.

```jsonc
{
  "generated_at": "2026-01-01T00:00:00Z",
  "scope":      { "hardware": "amd", "runs": "nightly", "description": "..." },
  "pipeline":   { "org": "vllm", "slug": "perf-eval", "url": "..." },
  "metric_meta": { "tput_per_gpu": { "label": "...", "unit": "tok/s/GPU", "better": "higher" } },
  "thresholds": { "perf_rel": 0.005, "accuracy_abs": 0.01 },
  // What the recipes say should run, for Coverage. The page cannot reach
  // GitHub, so the collector snapshots it into the store.
  "expected": {
    "recorded_at": "2026-01-01T00:00:00Z",
    "configs":  [{ "workload": "wl-mi355x", "run": "8k-in-1k-out-conc-128", "model": "org/Model",
                   "device": "mi355x", "precision": "fp8",
                   "parallelism": { "tensor_parallel_size": 8 }, "parallel_label": "TP8", "gpus": 8,
                   "isl": 8192, "osl": 1024, "conc": 128 }],
    "accuracy": [{ "workload": "wl-mi355x", "model": "org/Model",
                   "device": "mi355x", "task": "gsm8k" }]
  },
  // Each build a point names, once: series points carry only the build number.
  "builds": {
    "601": { "date": "2026-09-26T14:00:28Z", "nightly_date": "2026-09-25", "vllm_commit": "...",
             "build_commit": "...", "image": "...", "build_url": "..." }
  },
  // Every nightly build seen, newest first, with or without results, so the
  // page can say when the newest one failed or ran no AMD workload.
  "nightly_runs": [
    { "build": "603", "nightly_date": "2026-09-26", "date": "...", "state": "failed",
      "build_url": "...", "amd_results": 0, "amd_pending": [] }
  ],
  "models": [{
    "model": "org/Model",
    "perf_configs": [{
      "device": "mi355x", "isl": 8192, "osl": 1024, "conc": 128,
      "parallelism": { "tensor_parallel_size": 8 }, "parallel_label": "TP8", "gpus": 8,
      "label": "8K in / 1K out @ conc 128 (MI355X)",
      // The Buildkite job that ran this config in each build, so links open it.
      "jobs": { "601": "01a0dc52-..." },
      "metrics": {
        "tput_per_gpu": {
          "better": "higher", "label": "...", "unit": "tok/s/GPU",
          "series": [{ "build": "601", "value": 1200.0,
                       "completed_requests": 512, "failed_requests": 0 }]
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
