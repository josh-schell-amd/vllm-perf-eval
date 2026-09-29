# Development

How the code is laid out, how to check a change, and why a few non-obvious
choices were made: vendored Chart.js, the secret scan, and the type checkers.
For anyone changing the code. Agents (and people) changing the code or
running the workflows should also read [AGENTS.md](../AGENTS.md) for the rules
and the human-approved way to start a run.

## Layout

```
site/index.html              markup, and the Content-Security-Policy
site/app.css                 styles
site/js/analysis.js          pure logic: dates, overnight comparisons, verdicts
site/js/app.js               state, rendering and charts
site/js/theme.js             applies the saved theme before first paint
site/vendor/                 Chart.js and the AMD logo, with provenance
scripts/perf_eval/
  normalize.py               metric registry, AMD filter, event normalizers
  store.py                   events.jsonl: atomic writes, 30-day retention
  collect_artifacts.py       Buildkite REST -> canonical events
  aggregate.py               events.jsonl -> perf_eval.json
  merge_events.py            identity-based merge of two stores
  seal.py                    encrypts the payload with the dashboard login
  secrets_scan.py
scripts/build_site.py        site/ + sealed perf_eval.json -> _site/
data/                        generated; the store lives in the private repo, gitignored here
tests/
.github/workflows/           collect-and-deploy.yml, lint-and-test.yml, secrets-scan.yml
```

## Dependencies

- `uv.lock` pins every package, dependencies of dependencies included, so CI
  and your machine run identical tools. To upgrade one, run
  `uv lock --upgrade-package ruff` and commit the lock.
- Both workflows install with `uv sync --locked`, which fails if `uv.lock` is
  stale.
- The collect workflow adds `--no-dev`, so only the runtime packages
  (`requests`, `PyYAML`, `truststore`) run next to the Buildkite and write
  tokens.
- Actions are pinned to commit SHAs.

## Checks

```bash
pytest
node --test tests/js/*.test.js   # the page's logic; any Node 18+, no install
ruff check . && ruff format --check .
pyright                      # type check, gating in CI
ty check scripts tests       # second opinion, advisory
python scripts/perf_eval/secrets_scan.py
```

To run the pipeline locally against real data, see the
[quick start](../README.md#quick-start).

## Chart.js is vendored

The page's one third-party runtime dependency, **Chart.js 4.4.1**, is
committed to `site/vendor/` rather than loaded from cdnjs.

- **Why:** on a locked-down network a blocked CDN gives a blank chart with no
  visible explanation. A local copy also makes the dashboard work offline,
  including straight off disk.
- **Same bytes:** the vendored file is byte-identical to what cdnjs serves. It
  matches the Subresource Integrity digest cdnjs publishes,
  `sha384-bs/nf9FbdNouRbMiFcrcZfLXYPKiPaGVGplVbv7dLGECccEXDW+S3zjqSKR5ZEaD`.
- **Checked:** `site/vendor/README.md` records the provenance and verification
  commands, and `tests/test_vendored_assets.py` asserts the digest, so a
  swapped or truncated file fails CI.

### Why the licence file sits beside it

Chart.js is MIT licensed, and MIT requires its notice to accompany every copy
that is distributed. Loading from a CDN distributed nothing; committing the
file and publishing it to Pages makes this project a redistributor. The
minified bundle carries no licence banner, so the notice lives in
`site/vendor/chart.umd.min.js.LICENSE.txt`, `build_site.py` publishes it with
the bundle, and the tests assert it is complete and reaches the built site.
See [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md).

## Secret scanning

**`scripts/perf_eval/secrets_scan.py`** runs in CI on every push and pull
request (`secrets-scan.yml`), and is the same check you can run locally. It
detects **known token shapes only**: a fixed prefix, then at least N
characters from a known alphabet. That is all a GitHub or Buildkite token is,
so each provider is one line of data rather than a regular expression:

```python
TokenShape("Buildkite API token", "bkua_", 40, LOWER_HEX)
```

A test asserts the module contains no `re.compile` at all.

> [!IMPORTANT]
> The scan does not cover other providers' token formats, or git history: a
> credential committed and then deleted leaves a clean tree that passes.
> Enable GitHub **secret scanning with push protection** for those. It knows
> far more providers and rejects the push before a token lands. It is free on
> public repositories.

### Why there is no generic hex rule, and no gitleaks

The scan used to flag any run of 40+ hex characters. In practice that caught
git commit SHAs, not credentials, and needed three suppression mechanisms to
stay usable: an unreadable pinned-action regex, a list of context hints, and
putting `data/` on the path allowlist. Deleting the rule removed all three,
and `data/` is now scanned for real. A gitleaks job was removed too, as an
unpinned binary download duplicating what push protection does.

## Type checking

`ruff` handles lint and formatting but does not type check. Both configured
checkers report zero errors on the current tree.

- **`pyright` is the gate.** Pylance's engine *is* pyright and reads the same
  `typeCheckingMode`, so configuring it in `[tool.pyright]` makes the editor's
  verdict reproducible on the command line and in CI.
- **`ty` is advisory.** It is fast and found a real bug here, but at `0.0.x`
  its diagnostics shift between releases, so it runs with
  `continue-on-error: true` until it reaches a stable release.

### Why `standard` and not `strict`

| Mode | Errors |
|---|---|
| `standard` | 0 |
| `strict` | 2307 |

About 95% of that gap is six rules (`reportUnknownMemberType`,
`reportUnknownVariableType`, `reportUnknownArgumentType`,
`reportUnknownParameterType`, `reportMissingParameterType`,
`reportMissingTypeArgument`), which fire because this domain is JSON-shaped.
Writing `dict[str, Any]` everywhere would add no safety. The useful fix is
`TypedDict` definitions for the event and payload shapes, which is worth doing
but is a project, not a config flag.

### What the type checkers caught

One real defect in production code:

```python
# Before: the guard and the value are two separate lookups.
{e.get("device") for e in eval_events if e.get("device")}
```

Change one `.get()` and not the other and a `None` reaches `sorted()` as a
runtime `TypeError`. It is now a single bound lookup via a walrus.

The other ~20 findings were in tests that subscripted an `X | None` result
directly; adding `assert result is not None` gives a readable failure instead
of `TypeError: 'NoneType' object is not subscriptable`. Two signatures also
changed to match what they accept: `transform_perf(gpus: int | None)`, and the
fixtures' `nightly: object`.
