# Deploying

How to set up a new copy of this dashboard on GitHub: the secrets, Pages, and
push protection. For whoever owns the repository. Once set up, runs are
hourly (US Central working hours) and automatic; see
[data-pipeline.md](data-pipeline.md) for what each run does, and
[AGENTS.md](../AGENTS.md) for how to start one by hand.

The site repository is public and holds no data. Results live in Databricks
(`vllm_perf_data_ingest`, `vllm_eval_data_ingest`), owned by the `perf-eval`
pipeline, and the published site carries them only encrypted, sealed with a
shared login (see [The login](#the-login)).

## 1. Add the secrets

All go in the site repository: Settings → Secrets and variables → Actions →
New repository secret, or `gh secret set <NAME>`.

| Secret | Purpose | Scope needed |
|---|---|---|
| `DATABRICKS_HOST` | The Databricks workspace hostname to query | Read access to `vllm_perf_data_ingest` and `vllm_eval_data_ingest` |
| `DATABRICKS_WAREHOUSE_ID` | The SQL warehouse to run the query against | — |
| `DATABRICKS_TOKEN` | Authenticates the query | **Read-only**: `databricks_collect.py` only ever issues `SELECT`, and `tests/test_token_safety.py` asserts that, but a token scoped to write would still be more than it needs |
| `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD` | The login that seals the published data and opens it in the page | Choose a long password: the sealed file is public, so it can be guessed at offline |
| `HF_TOKEN` (optional) | Read a gated model's `config.json` on Hugging Face, for the expected-configs snapshot's precision | A Hugging Face read token |
| `GITHUB_TOKEN` | Read the public workload recipes | Provided by Actions; nothing to add |

Without `HF_TOKEN`, a gated model's precision in the Coverage card's expected
configs falls back to the recipe and its name, and may read "unstated" (this
does not affect published *results*, which trust Databricks's own resolved
`precision` field directly).

Then, in the site repository, Settings → Pages → Source → **GitHub Actions**.
The deploy job uploads the built site as an artifact and deploys it; no branch
holds it.

## 2. Turn on push protection

Enable GitHub **secret scanning with push protection** on the site
repository. The repository's own scan only knows GitHub token shapes and
does not look at git history; push protection covers both. See
[Secret scanning](development.md#secret-scanning).

## The login

GitHub Pages serves static files, so no server checks a password. Instead
`scripts/perf_eval/seal.py` encrypts the payload when the site is built
(PBKDF2-SHA256 with 600,000 iterations, then AES-256-GCM), and
`site/js/unlock.js` derives the same key in the browser from what the reader
types. The site publishes only `perf_eval.sealed.json`; `build_site.py`
refuses to build without the login, and fails if any published file contains
a model name or timestamp from the payload.

- **One shared login.** There are no per-person accounts, and no way to shut
  out one person but not the others.
- **The page keeps the key, not the password,** for the browser tab, so a
  reload does not ask again. *Sign out* forgets it.
- **Changing the password protects later data only.** Anyone who saved an
  earlier sealed file can still open it with the old password. After a change,
  run Collect and Deploy so the site is resealed; readers sign in again.

## How the credentials are kept safe

- Each is injected as step-scoped `env:` on only the steps that use it:
  `DATABRICKS_*` on Collect, the login on the change check and the build.
- `tests/test_token_safety.py` asserts only `databricks_collect.py` can read
  the Databricks token and never issues a write, and asserts where each
  secret may reach.
- No checkout persists credentials.
- The Actions logs of a public repository are public: the workflow logs
  counts and warnings, never results.
- Every push and pull request is secret-scanned.
