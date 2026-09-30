# Agent instructions for perf-eval-dashboard

A static dashboard of AMD nightly results from the `vllm/perf-eval` Buildkite
pipeline. An hourly GitHub Actions run (US Central working hours) downloads the nightly's result artifacts,
keeps 30 days of them in an event store, aggregates that into one JSON
payload, and deploys the page with it, encrypted, to GitHub Pages. There is
no server: the page asks for the shared login and decrypts its own payload.

`README.md` is the overview, and `docs/` is the reference for how everything
works: scope, regression rules (`docs/regression-detection.md`), the payload
contract and failure handling (`docs/data-pipeline.md`). This file covers
what an agent needs to change the code safely and to run the workflows.

## Where things live

| To change | Edit |
|---|---|
| Which results are collected, and how they are labeled | `scripts/perf_eval/collect_artifacts.py`, `normalize.py` |
| The event store: identity, retention, compaction | `scripts/perf_eval/store.py` |
| The payload the page reads | `scripts/perf_eval/aggregate.py`, then the page, then the payload section of `docs/data-pipeline.md` |
| A metric (label, unit, direction) | `METRIC_META` in `normalize.py`; the page reads it from the payload |
| What the page computes: dates, overnight comparisons, verdicts | `site/js/analysis.js`, with tests in `tests/js/` |
| What the page shows | `site/js/app.js`, `site/app.css`, `site/index.html` |
| How the site is assembled | `scripts/build_site.py` |
| The login: sealing and opening the payload | `scripts/perf_eval/seal.py`, `site/js/unlock.js`; they must agree on every parameter |
| The workflows | `.github/workflows/`; logic belongs in Python, not in YAML bash |

This repository is public and holds no data. The store is
`data/events.jsonl` on branch `dashboard-state` of the private repository
`josh-schell-amd/perf-eval-state`, and the site is deployed to Pages with
the payload sealed. To inspect the store, with access to that repository:
`git fetch <private remote> dashboard-state`, then
`git show FETCH_HEAD:data/events.jsonl`.

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

- **Never edit the data branches by hand.** The store only holds the 30 days
  a run can scan, so Buildkite is the source of truth. To fix stored results
  after changing how they are labeled, change the code, then run Collect and
  Deploy with `rebuild` (below). Don't push to `dashboard-state`.
- **Don't guess a label.** Derive it from what decides it (precision comes
  from the recipe, its `serve_args` and the checkpoint's `config.json`; see
  docs/data-pipeline.md). What nothing states is left empty (`""`), and the
  page shows it as "unstated". A guessed default mislabels results and splits
  their series when the guess is corrected.
- **Dates are Buildkite's.** A nightly is dated by its message ("Nightly run
  2026-09-25"), `nightly_date`, not by the day it finished (`date`, which
  orders and windows results). They differ: nightlies usually finish the next
  day.
- **The page and its payload always deploy together.** Don't add fallbacks for
  older payload shapes. Change `aggregate.py`, the page and the payload
  section of `docs/data-pipeline.md` in the same commit.
- **Scope is AMD nightly only**, enforced at collection and again at
  aggregation. Keep both.

## Rules that keep the data private

- **Never commit results, payloads or the store to this repository**, and
  never paste them into issues, commit messages or docs. It is public.
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

- Only `collect_artifacts.py` reads `BUILDKITE_TOKEN`, only with GET
  requests, and only the ingest step of `collect-and-deploy.yml` receives it.
  `tests/test_token_safety.py` enforces this; don't loosen it.
- `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` reach only the change check and
  the build; `STATE_REPO_TOKEN` only the store's checkout and push. The same
  test enforces both.
- No checkout persists credentials; the one step that pushes supplies the
  token for that push.

## Running the workflows (human-gated)

Runs are visible to the team, and Collect and Deploy writes the shared store
and the live site. **Never start, re-run or cancel a workflow run without the
user's explicit approval for that specific run.** A general go-ahead to work
on the code is not approval to run a workflow.

| Workflow | Runs on | Writes |
|---|---|---|
| `collect-and-deploy.yml` (Collect and Deploy) | hourly at :17, 14:00-21:00 UTC; pushes to `main` touching the page, the payload build, or itself; manual | from `main`: the store (private repo, `dashboard-state`) and the site (Pages); from another branch: nothing |
| `lint-and-test.yml` (Lint and Test) | pushes to `main`, pull requests, manual | nothing |
| `secrets-scan.yml` (Secrets Scan) | every push and pull request, manual | nothing |

Collect and Deploy's manual options:

- `rebuild` (default off): re-downloads the last 30 days of results with the
  current code and replaces the store. Use it after changing how results are
  labeled. It keeps the old store, and fails, if it finds fewer results.
- `deploy` (default on): publishes the site when done.

A manual run from a branch other than `main` is a **preview**: it collects
and builds, then stops before saving or publishing. Use one to check a change
against real data before merging.

### Procedure

1. **Propose the run and wait.** Tell the user the workflow, the branch, the
   inputs, and what it will write, as the exact command you will run. For
   example: "Run Collect and Deploy from `main` with `rebuild=true`. This
   re-downloads 30 days of results, replaces the store on `dashboard-state`,
   and redeploys the site." Then wait for an explicit yes.
2. **Make sure the code is on the remote.** Runs use the pushed branch; ask
   before pushing anything.
3. **Start it** with an authenticated `gh` (check `gh auth status` first):

   ```bash
   gh workflow run collect-and-deploy.yml --ref main -f rebuild=true -f deploy=true
   gh workflow run collect-and-deploy.yml --ref my-branch   # a preview
   gh workflow run lint-and-test.yml --ref my-branch
   ```

4. **Report the run URL immediately**, from
   `gh run list --workflow collect-and-deploy.yml --limit 1`.
5. **Watch it and report the outcome:** `gh run watch <run-id>`, then
   `gh run view <run-id> --log-failed` for a failure. A healthy rebuild's
   ingest step ends with "Rebuilt the store: N results (was M)".

Don't:

- Run a rebuild to "see if it helps". Say what it should change, and why.
- Re-run a failed run before reading its log.
- Cancel a run you did not start.
- Start a run while another Collect and Deploy is in progress. Runs queue
  behind each other, and yours would act on the store the first one leaves.

## Commits

- Commit messages say what changed and why, in plain sentences.
- Keep README and `docs/` in sync in the same commit whenever a change touches the
  payload, the layout, the workflows, or anything a new reader relies on.
- Comments explain why, briefly. If a few lines need a long comment, simplify
  the code instead.
- Disclose AI assistance with a `Co-Authored-By:` trailer naming the model.
