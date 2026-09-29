# Perf Eval Dashboard

A static dashboard for AMD nightly performance and accuracy results from the
[`vllm/perf-eval`](https://buildkite.com/vllm/perf-eval) Buildkite pipeline.

Live: <https://josh-schell-amd.github.io/vllm-perf-eval/> (sign-in required)

> [!IMPORTANT]
> This is an experimental demo. It is not a supported or authoritative source
> of performance or accuracy results, and should not be relied on beyond
> demonstration purposes.

## What this dashboard covers

AMD nightlies only. If a run looks missing, check it is in scope first.

| | Included | Excluded |
|---|---|---|
| Hardware | AMD MI-series (`mi300x`, `mi355x`, …) | NVIDIA (H200, B200, A100) |
| Runs | Scheduled nightlies | Ad-hoc and pull-request builds |
| Branch | `main` | Every other branch |
| Build state | `finished` | Running, cancelled, failed-to-start |

- **NVIDIA** results are on [perf.vllm.ai](https://perf.vllm.ai).
- **Nightlies only**, because each runs the full matrix and so compares with
  the last. An ad-hoc build may cover one workload at one concurrency.

## How it works

- **One page, no build step.** The frontend is `site/`: `index.html`, a
  stylesheet and plain JavaScript files, plus a vendored copy of Chart.js. No
  framework and no bundler.
- **One data file, behind a login.** The page fetches a single
  `perf_eval.sealed.json` published next to it: the payload encrypted with a
  shared login, which the page asks for and decrypts. The repository is public
  and holds no results; they live in a private repository
  ([deploying.md](docs/deploying.md)).
- **Nothing fetched at runtime** beyond that file. No CDN, no fonts, no
  analytics, no trackers; the page's Content-Security-Policy enforces it.
- **Collected once a day** (17:17 UTC) from Buildkite by a GitHub Actions
  workflow, and served from GitHub Pages.

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

Three rules explain most of what you will see:

1. **The store is a 30-day cache, rebuilt from Buildkite.** Buildkite is the
   source of truth. The store, the payload and the page all keep 30 days, and
   a `rebuild` run re-downloads them. Never edit the data branches by hand.
2. **Regressions are overnight only.** The newest nightly is compared with
   the one before it. A config that skipped either night is not counted; it
   shows in Coverage instead.
3. **Thresholds decide what counts.** A perf metric must move at least
   **0.5%**, an accuracy score at least **1 point**. Smaller moves are neutral.
   There is no smoothing.

Details: [data pipeline](docs/data-pipeline.md),
[regression detection](docs/regression-detection.md).

## Quick start

### Set up

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv sync                  # creates .venv with exactly what uv.lock pins
. .venv/bin/activate     # Windows: .venv\Scripts\activate
```

`uv.lock` pins every package, so CI and your machine run identical tools.

### Run the pipeline against real data

This is the sequence the workflow runs, minus the branch commits. It is
read-only against Buildkite, and ingest only appends what is new to
`data/events.jsonl`, so it is safe to re-run as often as you like.

```bash
export BUILDKITE_TOKEN=bkua_...          # read-only: Read Builds + Read Artifacts

python scripts/perf_eval/collect_artifacts.py   # the last 30 days, the default
python scripts/perf_eval/aggregate.py
python scripts/build_site.py
python -m http.server --directory _site 8000
```

Then open <http://localhost:8000>.

- Only ingest (`collect_artifacts.py`) needs the Buildkite token.
  `aggregate.py` and `build_site.py` read the local store.
- The workload recipes come from the public `vllm-project/perf-eval` repo, so
  no GitHub token is needed. If you hit GitHub's anonymous rate limit,
  `export GITHUB_TOKEN="$(gh auth token)"`.
- Add `--dry-run` to the collector to see what it would download, with no
  downloads and no writes.

### Run the checks

```bash
pytest
node --test tests/js/*.test.js   # the page's logic; any Node 18+, no install
ruff check . && ruff format --check .
pyright                      # type check, gating in CI
ty check scripts tests       # second opinion, advisory
python scripts/perf_eval/secrets_scan.py
```

## Reading the dashboard

- **KPI cards** answer "is tonight's build healthy?": the latest nightly
  with AMD results (and each later day that had none, and why), overnight
  performance, regressions, improvements, accuracy, and coverage.
- **Tabs:** Performance (default), Trends, Throughput vs Latency, Accuracy,
  and Data (every run in one sortable table, with Copy CSV). The Missing
  panel at the bottom lists later nightlies with no AMD results, and configs
  the recipes expect but the newest build did not report.
- **Red means regression, and nothing else.** Green means improvement.
- On the *Performance overnight* card, green is any gain in the median and
  red a drop past the threshold. It is not a regression alarm.
- Build links open the Buildkite job that ran that model, not just the build.
- Throughput here is **per GPU**, so it is ATOM's number divided by the GPU
  count. Check this before comparing the two dashboards.
- Filters, tab, metric and chart window live in the URL, so **Copy link**
  reproduces the view.

Full guide: [Reading the dashboard](docs/reading-the-dashboard.md).

## Where to go next

| If you want to | Read |
|---|---|
| Understand every card, tab and colour | [docs/reading-the-dashboard.md](docs/reading-the-dashboard.md) |
| Know exactly what counts as a regression, and why | [docs/regression-detection.md](docs/regression-detection.md) |
| Follow the data: schedule, failures, cost, retention, the payload contract | [docs/data-pipeline.md](docs/data-pipeline.md) |
| Deploy your own copy: token, branches, Pages, push protection | [docs/deploying.md](docs/deploying.md) |
| Change the code: layout, checks, vendoring, secret scanning, type checking | [docs/development.md](docs/development.md) |
| Change code or run workflows as an agent, or start a run safely | [AGENTS.md](AGENTS.md) |

## Feedback

Questions, bugs or ideas: Josh Schell, [josh.schell@amd.com](mailto:josh.schell@amd.com).
The dashboard's footer links the same address.

## Licence

MIT. See [LICENSE](LICENSE). Copyright © Advanced Micro Devices, Inc.,
matching [ROCm/ATOM](https://github.com/ROCm/ATOM).

Bundled third-party software and its required notices are recorded in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The only bundled library is
Chart.js, also MIT, so there is no licence conflict.

## Relationship to `vllm-ci-dashboard`

This dashboard replaces the `Perf Eval` tab in `vllm-ci-dashboard`, where the
same feature lived inside a ~13k-line shared frontend module. The two run in
parallel for now; retiring the old tab is tracked separately.
