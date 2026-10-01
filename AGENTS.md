# Agent instructions for perf-eval-dashboard

A static dashboard of AMD nightly results from the `vllm/perf-eval` Buildkite
pipeline. An hourly GitHub Actions run (US Central working hours) queries
that pipeline's results straight from Databricks (`vllm_perf_data_ingest`,
`vllm_eval_data_ingest` — full history, nothing stored locally between runs),
aggregates that into one JSON payload, and deploys the page with it,
encrypted, to GitHub Pages. There is no server: the page asks for the shared
login and decrypts its own payload.

`README.md` is the overview, and `docs/` is the reference for how everything
works: scope, regression rules (`docs/regression-detection.md`), the payload
contract and failure handling (`docs/data-pipeline.md`). This file covers
what an agent needs to change the code safely and to run the workflows.

## Where things live

| To change | Edit |
|---|---|
| Which results are collected, and how they are labeled | `scripts/perf_eval/databricks_collect.py`, `normalize.py` |
| Workload recipes: coverage snapshot, accuracy model/device join | `scripts/perf_eval/recipes.py` |
| Event timestamps, nightly identity, JSONL I/O | `scripts/perf_eval/events.py` |
| The payload the page reads | `scripts/perf_eval/aggregate.py`, then the page, then the payload section of `docs/data-pipeline.md` |
| A metric (label, unit, direction) | `METRIC_META` in `normalize.py`; the page reads it from the payload |
| What the page computes: dates, overnight comparisons, verdicts | `site/js/analysis.js`, with tests in `tests/js/` |
| What the page shows | `site/js/app.js`, `site/app.css`, `site/index.html` |
| How the site is assembled | `scripts/build_site.py` |
| The login: sealing and opening the payload | `scripts/perf_eval/seal.py`, `site/js/unlock.js`; they must agree on every parameter |
| The workflows | `.github/workflows/`; logic belongs in Python, not in YAML bash |

This repository is public and holds no data. Results live in Databricks
(`vllm_perf_data_ingest`, `vllm_eval_data_ingest`), owned by the `perf-eval`
pipeline and queried fresh every run — there is no local event store to
inspect or push to — and the site is deployed to Pages with the payload
sealed.

## Checks before every commit

```bash
pytest -q
node --test tests/js/*.test.js
ruff check . && ruff format --check .
pyright
python scripts/perf_eval/secrets_scan.py
```

CI runs these in `lint-and-test.yml` (plus `ty`, advisory) and the secrets
scan in `secrets-scan.yml`. The Node tests need any Node 18+;
without one installed, VS Code's bundled runtime works:
`ELECTRON_RUN_AS_NODE=1 "<path to Code.exe>" --test tests/js/*.test.js`.

A change to the page is not verified until it has rendered. Build it from a
real payload, serve it, and load it in a browser (headless is fine), then
check the console for errors and Content-Security-Policy violations:

```bash
python scripts/perf_eval/aggregate.py --store <events.jsonl> --output /tmp/perf_eval.json
DASHBOARD_USERNAME=viewer DASHBOARD_PASSWORD=local-test \
  python scripts/build_site.py --payload /tmp/perf_eval.json --output /tmp/site
python -m http.server 8765 --directory /tmp/site
```

## Rules that keep the data right

- **Databricks is the only source of truth for results.** There is no local
  store to edit or migrate: every run re-queries `vllm_perf_data_ingest` /
  `vllm_eval_data_ingest` and rebuilds the payload from scratch. To fix how
  historical results are labeled, change the code and run Collect and Deploy
  (below) — it always re-derives everything, there is no separate rebuild
  mode anymore.
- **Trust Databricks's own resolved fields.** Perf rows already carry
  `precision`, `tp`/`ep`/`dp_attention`, and pre-converted per-GPU metrics;
  don't re-derive them from a recipe (see `docs/data-pipeline.md`'s Data
  identity section for the one known gap this creates). Accuracy rows carry
  neither `model` nor `device` — those come from a recipe join on `workload`,
  the one case where the code still reads a recipe for labeling.
- **A nightly's identity is a calendar-day bucket** of its ingest timestamp,
  not a Buildkite build number or message-parsed date. See
  `docs/data-pipeline.md`'s Data identity section for what this costs (a
  nightly retried on a different day is not folded back into the original).
- **The page and its payload always deploy together.** Don't add fallbacks for
  older payload shapes. Change `aggregate.py`, the page and the payload
  section of `docs/data-pipeline.md` in the same commit.
- **Scope is AMD nightly only**, enforced at collection and again at
  aggregation. Keep both.

## Rules that keep the data private

- **Never commit results, payloads or `data/events.jsonl` to this
  repository**, and never paste them into issues, commit messages or docs.
  It is public.
- **Never log values.** Actions logs of a public repository are public: log
  counts, names and warnings only.
- **Nothing reaches the site unsealed.** `build_site.py` publishes only the
  sealed payload and fails if a site file contains data from it; keep both.

## Rules that keep the page safe

- Everything interpolated into HTML goes through `esc()`; every link through
  `safeUrl()`.
- No inline `<script>`: the Content-Security-Policy in `index.html` allows
  scripts only from the site itself. New code goes in `site/js/`, and
  `build_site.py` cache-tags every local script and stylesheet.
- Logic without DOM access goes in `analysis.js`, with a test.

## Rules for the credentials

- Only `databricks_collect.py` reads `DATABRICKS_HOST`/`DATABRICKS_WAREHOUSE_ID`/
  `DATABRICKS_TOKEN`, only to run `SELECT` queries, and only the Collect step
  of `collect-and-deploy.yml` receives them. `tests/test_token_safety.py`
  enforces this; don't loosen it.
- `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` reach only the change check and
  the build. The same test enforces it.
- No checkout persists credentials.

## Running the workflows (human-gated)

Runs are visible to the team, and Collect and Deploy deploys the live site.
**Never start, re-run or cancel a workflow run without the user's explicit
approval for that specific run.** A general go-ahead to work on the code is
not approval to run a workflow.

| Workflow | Runs on | Writes |
|---|---|---|
| `collect-and-deploy.yml` (Collect and Deploy) | hourly at :17, 14:00-21:00 UTC; pushes to `main` touching the page, the payload build, or itself; manual | from `main`: the site (Pages); from another branch: nothing |
| `lint-and-test.yml` (Lint and Test) | pushes to `main`, pull requests, manual | nothing |
| `secrets-scan.yml` (Secrets Scan) | every push and pull request, manual | nothing |

Collect and Deploy's manual option:

- `deploy` (default on): publishes the site when done.

There is no rebuild option: every run re-queries Databricks and re-derives
the whole payload, so there is nothing separate to rebuild.

A manual run from a branch other than `main` is a **preview**: it collects
and builds, then stops before publishing. Use one to check a change against
real data before merging.

### Procedure

1. **Propose the run and wait.** Tell the user the workflow, the branch, and
   what it will write, as the exact command you will run. For example: "Run
   Collect and Deploy from `main`. This re-queries Databricks and redeploys
   the site." Then wait for an explicit yes.
2. **Make sure the code is on the remote.** Runs use the pushed branch; ask
   before pushing anything.
3. **Start it** with an authenticated `gh` (check `gh auth status` first):

   ```bash
   gh workflow run collect-and-deploy.yml --ref main
   gh workflow run collect-and-deploy.yml --ref my-branch   # a preview
   gh workflow run lint-and-test.yml --ref my-branch
   ```

4. **Report the run URL immediately**, from
   `gh run list --workflow collect-and-deploy.yml --limit 1`.
5. **Watch it and report the outcome:** `gh run watch <run-id>`, then
   `gh run view <run-id> --log-failed` for a failure.

Don't:

- Re-run a failed run before reading its log.
- Cancel a run you did not start.
- Start a run while another Collect and Deploy is in progress. Runs queue
  behind each other.

## Commits

- Commit messages say what changed and why, in plain sentences.
- Keep README and `docs/` in sync in the same commit whenever a change touches the
  payload, the layout, the workflows, or anything a new reader relies on.
- Comments explain why, briefly. If a few lines need a long comment, simplify
  the code instead.
- Disclose AI assistance with a `Co-Authored-By:` trailer naming the model.
