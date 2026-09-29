# Deploying

How to set up a new copy of this dashboard on GitHub: the secrets, the
private store, Pages, and push protection. For whoever owns the repository.
Once set up, runs are daily and automatic; see
[data-pipeline.md](data-pipeline.md) for what each run does, and
[AGENTS.md](../AGENTS.md) for how to start one by hand.

The site repository is public and holds no data. Results live in a separate
**private** repository, and the published site carries them only encrypted,
sealed with a shared login (see [The login](#the-login)).

## 1. Add the secrets

All go in the site repository: Settings → Secrets and variables → Actions →
New repository secret, or `gh secret set <NAME>`.

| Secret | Purpose | Scope needed |
|---|---|---|
| `BUILDKITE_TOKEN` | List builds, list and download artifacts, read each job's image | **Read-only**: Read Builds, Read Artifacts, Read Job Env |
| `STATE_REPO_TOKEN` | Check out and push the event store in the private repository | A fine-grained token for that repository only: Contents read and write |
| `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD` | The login that seals the published data and opens it in the page | Choose a long password: the sealed file is public, so it can be guessed at offline |
| `HF_TOKEN` (optional) | Read a gated model's `config.json` on Hugging Face, for its precision | A Hugging Face read token |
| `GITHUB_TOKEN` | Read the public workload recipes | Provided by Actions; nothing to add |

Without Read Job Env on the Buildkite token, every image reads "unstated".
Without `HF_TOKEN`, a gated model's precision falls back to the recipe and its
name, and may read "unstated".

> [!WARNING]
> Use a **read-only** Buildkite token. The collector only issues GETs, and
> `tests/test_token_safety.py` asserts that, but a token with write scopes
> would still be more than it needs.

## 2. Create the private store

The workflow checks out branch `dashboard-state` of the private repository
named by `STATE_REPO` in `collect-and-deploy.yml`, and fails if it is missing,
rather than starting from an empty store that would later overwrite the real
one. In a new private repository, create it once, empty:

```bash
git switch --orphan dashboard-state
mkdir -p data && : > data/events.jsonl && git add -f data/events.jsonl
git commit -m "Empty event store" && git push origin dashboard-state
```

Then, in the site repository, Settings → Pages → Source → **GitHub Actions**.
The deploy job uploads the built site as an artifact and deploys it; no branch
holds it.

## 3. Turn on push protection

Enable GitHub **secret scanning with push protection** on the site
repository. The repository's own scan only knows GitHub and Buildkite token
shapes and does not look at git history; push protection covers both. See
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
  `BUILDKITE_TOKEN` on ingest, the login on the change check and the build,
  `STATE_REPO_TOKEN` on the store's checkout and push.
- `tests/test_token_safety.py` pins the Buildkite org to `vllm`, asserts only
  `collect_artifacts.py` can read the Buildkite token and never issues a
  write, and asserts where each secret may reach.
- No checkout persists credentials; the one step that pushes supplies the
  token for that push.
- The Actions logs of a public repository are public: the workflow logs
  counts and warnings, never results.
- Every push and pull request is secret-scanned.
