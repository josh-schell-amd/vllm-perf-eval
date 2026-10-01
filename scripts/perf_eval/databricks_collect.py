#!/usr/bin/env python3
"""Collect AMD nightly perf-eval results from Databricks into an event file.

Scope: AMD workloads (``is_amd_workload``), nightly rows only (``nightly``
field, stamped by the ingest scripts in the sibling ``perf-eval`` repo).

Both source tables (``vllm_perf_data_ingest``, ``vllm_eval_data_ingest``) are
Zerobus event logs with two VARIANT columns: ``message`` (the JSON body the
ingest script POSTed) and ``request_metadata`` (HTTP request info, including
an ingest ``timestamp``). Every row's real fields live inside ``message``.

Unlike the old Buildkite-artifact collector, there is no local event store:
Databricks already retains full history, so every run queries it fresh and
rebuilds the payload from scratch. There is no Buildkite build number on
these rows either, so identity is a calendar-day bucket of each row's own
ingest timestamp instead — a nightly retried on a different day is not
folded back into the original one. See docs/data-pipeline.md.

``vllm_perf_data_ingest`` rows carry per-GPU metrics pre-converted to
seconds, which are used as they are. Their ``precision`` and ``tp`` are not:
perf-eval stamps ``precision`` from a model-name marker, falling back to
``bf16`` (so an int4 or fp8 checkpoint with no marker reads ``bf16``), and
``tp`` is TP x DP as one number. Both come from the matching workload recipe
instead (``recipe_labels``), the same derivation the coverage card's
expected configs use, so results and expectations agree.
``vllm_eval_data_ingest`` rows carry real Buildkite identity but no
``model``/``device``; those are recovered from the workload recipe too.
Recipes are read at ``main``.

Queries go through the SQL Statement Execution REST API, not
databricks-sql-connector: one documented HTTP call, no Thrift client in the
job that holds the token (connector 4.6's Thrift paging also lost rows).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perf_eval import WINDOW_DAYS  # noqa: E402
from perf_eval.events import (  # noqa: E402
    EXPECTED_CONFIGS_EVENT,
    write_events_atomic,
)
from perf_eval.normalize import (  # noqa: E402
    is_amd_workload,
    parallel_key,
    perf_metrics,
    score_rows,
    to_int,
    utcnow_iso,
)
from perf_eval.recipes import (  # noqa: E402
    expected_accuracy,
    expected_configs,
    fetch_workload_map,
    use_system_certificates,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUTPUT = ROOT / "data" / "events.jsonl"

PERF_TABLE = "vllm_perf_data_ingest"
EVAL_TABLE = "vllm_eval_data_ingest"

# Recipes are always read at main: there is no per-nightly Buildkite commit
# to pin to anymore (see recipes.py's module docstring).
RECIPE_REF = "main"

# Both observed image-tag conventions end in a bare hex commit run:
#   public.ecr.aws/.../vllm-release-repo:<sha>-x86_64
#   vllm/vllm-openai-rocm:nightly-<sha>
_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}", re.IGNORECASE)


def commit_from_image(image: str) -> str:
    """The longest hex run in an image tag, as a best-effort vLLM commit."""
    matches = _COMMIT_RE.findall(image or "")
    return max(matches, key=len).lower() if matches else ""


def is_nightly_row(row: dict) -> bool:
    """Whether a Databricks row is from a scheduled nightly, not an ad-hoc run.

    The ingest scripts in the sibling ``perf-eval`` repo stamp ``nightly: true``
    only when ``NIGHTLY=1`` was set on the build; a missing or falsy value
    (including any non-boolean JSON value) means an ad-hoc run.
    """
    return row.get("nightly") is True


def day_bucket(timestamp: str) -> str:
    """The calendar-day identity key (``YYYY-MM-DD``) from an ISO-ish
    timestamp. Both tables' timestamps (``2026-09-30 17:44:42`` and
    ``2026-09-30T17:44:42.255335868Z``) start with the date, so a slice is
    enough — no timezone conversion, since both are already UTC."""
    return str(timestamp or "").strip()[:10]


def perf_parallelism(row: dict) -> dict:
    """A parallelism dict from the row's own scalars, not a recipe join.

    ``tp`` here is TP x DP as a single int (see ``lib/ingest_perf.py`` in the
    sibling repo), not vLLM's individual flags, so this cannot distinguish a
    recipe using pipeline or prefill-context parallelism from one that
    doesn't; accepted gap, see docs/data-pipeline.md.
    """
    parallelism: dict = {}
    tp = to_int(row.get("tp")) or 1
    if tp > 1:
        parallelism["tensor_parallel_size"] = tp
    ep = to_int(row.get("ep")) or 1
    if ep > 1:
        parallelism["enable_expert_parallel"] = True
    if str(row.get("dp_attention", "")).strip().lower() == "true":
        parallelism["dp_attention"] = True
    return parallelism


def _recipe_shape(model, device, isl, osl, conc, tp) -> tuple:
    return (
        str(model or "").strip(),
        str(device or "").strip(),
        to_int(isl),
        to_int(osl),
        to_int(conc),
        to_int(tp),
    )


def recipe_labels(recipes: dict[str, tuple[dict, dict]]) -> dict[tuple, tuple[str, dict]]:
    """(precision, parallelism) per recipe config, keyed on what a perf row
    carries: model, device, ISL/OSL, concurrency and perf-eval's ``tp``.

    A shape two recipes share with different labels is left out, so its rows
    keep the row's own TP and an unstated precision rather than a guess.
    """
    found: dict[tuple, set] = {}
    labels: dict[tuple, tuple[str, dict]] = {}
    for entry, configs in recipes.values():
        for config in configs.values():
            shape = _recipe_shape(
                entry.get("model"),
                entry.get("device"),
                config.get("isl"),
                config.get("osl"),
                config.get("conc"),
                entry.get("bench_tp"),
            )
            label = (entry.get("precision") or "", entry.get("parallelism") or {})
            found.setdefault(shape, set()).add((label[0], parallel_key(label[1])))
            labels[shape] = label
    return {shape: label for shape, label in labels.items() if len(found[shape]) == 1}


def perf_event(
    row: dict, *, labels: dict[tuple, tuple[str, dict]], drops: Counter | None = None
) -> dict | None:
    """A canonical ``perf_result`` event from a ``vllm_perf_data_ingest`` row,
    labeled from its recipe (``recipe_labels``). ``drops`` counts why rows
    were left out, for the run log."""
    drops = drops if drops is not None else Counter()
    if not is_nightly_row(row):
        drops["perf: not nightly"] += 1
        return None
    device = str(row.get("device") or "").strip()
    image = str(row.get("image") or "").strip()
    if not is_amd_workload(image=image, device=device):
        drops["perf: not AMD"] += 1
        return None
    metrics = perf_metrics(row)
    if not metrics:
        drops["perf: no metrics"] += 1
        return None
    date = str(row.get("date") or "").strip()
    day = day_bucket(date)
    if not day:
        drops["perf: no date"] += 1
        return None
    model = str(row.get("model") or "").strip()
    label = labels.get(
        _recipe_shape(model, device, row.get("isl"), row.get("osl"), row.get("conc"), row.get("tp"))
    )
    if label is None:
        drops["perf: kept, no matching recipe (precision unstated)"] += 1
    precision, parallelism = label if label else ("", perf_parallelism(row))
    return {
        "event": "perf_result",
        "received_at": utcnow_iso(),
        "nightly": True,
        "model": model,
        "device": device,
        "precision": precision,
        "parallelism": parallelism,
        "isl": row.get("isl"),
        "osl": row.get("osl"),
        "conc": row.get("conc"),
        "date": date,
        "nightly_date": day,
        "build_number": day,
        "build_url": "",
        "build_commit": "",
        "branch": "",
        "image": image,
        "vllm_commit": commit_from_image(image),
        "completed_requests": None,
        "failed_requests": None,
        "metrics": metrics,
    }


def accuracy_event(
    row: dict, *, recipes: dict[str, tuple[dict, dict]], drops: Counter | None = None
) -> dict | None:
    """A canonical ``accuracy_result`` event from a ``vllm_eval_data_ingest`` row.

    ``vllm_eval_data_ingest`` rows carry real Buildkite identity but no
    ``model``/``device``; both are recovered from the workload recipe.
    ``drops`` counts why rows were left out, for the run log.
    """
    drops = drops if drops is not None else Counter()
    if row.get("kind") != "results":
        drops["eval: not a results row"] += 1
        return None
    if not is_nightly_row(row):
        drops["eval: not nightly"] += 1
        return None
    workload = str(row.get("workload") or "").strip()
    task = str(row.get("task") or "").strip()
    image = str(row.get("image") or "").strip()
    recipe = recipes.get(workload)
    if recipe is None:
        drops["eval: workload not in recipes"] += 1
    entry = recipe[0] if recipe else {}
    device = str(entry.get("device") or "").strip()
    model = str(entry.get("model") or "").strip()
    if not is_amd_workload(workload=workload, image=image, device=device):
        drops["eval: not AMD"] += 1
        return None
    rows = score_rows(
        [
            {"task": task_name, "metric": metric, "value": value}
            for task_name, metrics in ((row.get("data") or {}).get("results") or {}).items()
            if isinstance(metrics, dict)
            for metric, value in metrics.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
    )
    if not rows:
        drops["eval: no scores"] += 1
        return None
    build_number = str(row.get("buildkite_build_number") or "").strip()
    day = day_bucket(str(row.get("_ingest_timestamp") or ""))
    if not day:
        drops["eval: no date"] += 1
        return None
    return {
        "event": "accuracy_result",
        "received_at": utcnow_iso(),
        "nightly": True,
        "model": model,
        "workload": workload,
        "task": task,
        "device": device,
        "date": str(row.get("_ingest_timestamp") or ""),
        "nightly_date": day,
        "build_number": day,
        "build_url": str(row.get("buildkite_build_url") or "")
        or (f"https://buildkite.com/vllm/perf-eval/builds/{build_number}" if build_number else ""),
        "build_commit": str(row.get("buildkite_commit") or "").strip(),
        "branch": str(row.get("buildkite_branch") or "").strip(),
        "image": image,
        "vllm_commit": str(row.get("vllm_commit") or "").strip(),
        "results": rows,
    }


# ---------------------------------------------------------------------------
# Databricks I/O
# ---------------------------------------------------------------------------


STATEMENTS_API = "/api/2.0/sql/statements"
HTTP_TIMEOUT = 60
QUERY_TIMEOUT = 600
POLL_SECONDS = 5

# Eval "results" rows carry lm-eval's whole output (configs, per-task
# settings); only these fields are read, so only these are fetched.
EVAL_FIELDS = (
    "kind",
    "nightly",
    "workload",
    "task",
    "image",
    "vllm_commit",
    "buildkite_build_number",
    "buildkite_build_url",
    "buildkite_commit",
    "buildkite_branch",
    "data.results",
)


def _base_url() -> str:
    # The connector took a bare hostname, so the secret may be one.
    host = os.environ["DATABRICKS_HOST"].strip().rstrip("/")
    return host if host.startswith("https://") else f"https://{host}"


def run_query(statement: str, parameters: dict[str, str]) -> list[list]:
    """Every row of a read-only statement, via the SQL Statement Execution API.

    Waits for the statement, then follows every result chunk, and fails if
    the rows received are not all the rows the manifest reports.
    """
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {os.environ['DATABRICKS_TOKEN']}"
    base = _base_url()
    resp = session.post(
        base + STATEMENTS_API,
        json={
            "warehouse_id": os.environ["DATABRICKS_WAREHOUSE_ID"],
            "statement": statement,
            "parameters": [{"name": k, "value": v} for k, v in parameters.items()],
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
            "wait_timeout": "50s",
            "on_wait_timeout": "CONTINUE",
        },
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    deadline = time.monotonic() + QUERY_TIMEOUT
    while body["status"]["state"] in ("PENDING", "RUNNING"):
        if time.monotonic() > deadline:
            raise TimeoutError(f"Databricks query still running after {QUERY_TIMEOUT}s")
        time.sleep(POLL_SECONDS)
        resp = session.get(f"{base}{STATEMENTS_API}/{body['statement_id']}", timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
    state = body["status"]["state"]
    if state != "SUCCEEDED":
        error = (body["status"].get("error") or {}).get("message", "no error message")
        raise RuntimeError(f"Databricks query {state}: {error}")

    manifest = body.get("manifest") or {}
    if manifest.get("truncated"):
        raise RuntimeError("Databricks truncated the result; narrow the query")
    result = body.get("result") or {}
    rows = list(result.get("data_array") or [])
    while link := result.get("next_chunk_internal_link"):
        resp = session.get(base + link, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        rows.extend(result.get("data_array") or [])
    expected = manifest.get("total_row_count")
    if expected is not None and len(rows) != expected:
        raise RuntimeError(f"Databricks reported {expected} rows but {len(rows)} arrived")
    return rows


def fetch_rows(table: str, *, since: datetime, fields: tuple[str, ...] | None = None) -> list[dict]:
    """Nightly rows' ``message`` since ``since``, with the ingest
    ``timestamp`` as ``_ingest_timestamp``. ``fields`` (dotted paths) limits
    the message to those fields; ``None`` fetches all of it.

    Values come back through ``to_json``, so each is the exact JSON the ingest
    script sent: the strict client-side checks (``is_nightly_row``) still see
    a boolean ``true`` apart from a string ``"true"``. VARIANT paths need a
    ``::`` cast to be compared (``DATATYPE_MISMATCH`` otherwise).
    """
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%S")
    columns = ["to_json(message)"] if fields is None else [f"to_json(message:{f})" for f in fields]
    # Nightly rows only, server-side, so less is transferred; an eval
    # table's per-question "samples" rows are dropped there too.
    where = [
        "request_metadata:timestamp::string >= :cutoff",
        "try_cast(message:nightly AS BOOLEAN)",
    ]
    if table == EVAL_TABLE:
        where.append("message:kind::string = 'results'")
    statement = (
        f"SELECT request_metadata:timestamp::string, {', '.join(columns)} "  # noqa: S608
        f"FROM {table} WHERE {' AND '.join(where)}"
    )

    out = []
    for ingest_ts, *values in run_query(statement, {"cutoff": cutoff}):
        if fields is None:
            row = json.loads(values[0]) if values[0] else None
        else:
            row = {}
            for path, value in zip(fields, values, strict=True):
                if value is None:
                    continue
                *parents, leaf = path.split(".")
                node = row
                for parent in parents:
                    node = node.setdefault(parent, {})
                node[leaf] = json.loads(value)
        if not isinstance(row, dict):
            continue
        ingest_ts = str(ingest_ts or "")
        if ingest_ts and ingest_ts < cutoff:
            continue
        row["_ingest_timestamp"] = ingest_ts
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def buildkite_builds(eval_rows: list[dict]) -> dict[str, dict]:
    """The perf-eval Buildkite build that ran each vLLM commit, from eval rows.

    Perf rows carry no build, but a nightly's perf and eval jobs share one
    image, so its commit names the build. A commit two builds ran (a retry)
    is left out; its results keep the day as their build.
    """
    found: dict[str, dict[str, dict]] = {}
    for row in eval_rows:
        commit = str(row.get("vllm_commit") or "").strip()
        number = str(row.get("buildkite_build_number") or "").strip()
        if not commit or not number or not is_nightly_row(row):
            continue
        found.setdefault(commit, {})[number] = {
            "build_number": number,
            "build_url": str(row.get("buildkite_build_url") or "").strip()
            or f"https://buildkite.com/vllm/perf-eval/builds/{number}",
            "build_commit": str(row.get("buildkite_commit") or "").strip(),
            "branch": str(row.get("buildkite_branch") or "").strip(),
        }
    return {commit: next(iter(by.values())) for commit, by in found.items() if len(by) == 1}


def collect(*, days: int, gh_token: str, dry_run: bool = False) -> list[dict]:
    if not 1 <= days <= WINDOW_DAYS:
        raise ValueError(f"perf-eval lookback must be between 1 and {WINDOW_DAYS} days")

    since = datetime.now(UTC) - timedelta(days=days)
    perf_rows = fetch_rows(PERF_TABLE, since=since)
    eval_rows = fetch_rows(EVAL_TABLE, since=since, fields=EVAL_FIELDS)
    log.info(
        "Fetched %d %s rows and %d %s rows since %s",
        len(perf_rows),
        PERF_TABLE,
        len(eval_rows),
        EVAL_TABLE,
        since.isoformat(),
    )

    recipes = fetch_workload_map(gh_token, ref=RECIPE_REF)

    labels = recipe_labels(recipes)
    drops: Counter = Counter()
    events: list[dict] = []
    for row in perf_rows:
        event = perf_event(row, labels=labels, drops=drops)
        if event is not None:
            events.append(event)
    for row in eval_rows:
        event = accuracy_event(row, recipes=recipes, drops=drops)
        if event is not None:
            events.append(event)
    builds = buildkite_builds(eval_rows)
    named = 0
    for event in events:
        build = builds.get(event.get("vllm_commit") or "")
        if build:
            event.update(build)
            named += 1
    # Counts only: this log is public.
    for reason, count in sorted(drops.items()):
        log.info("%s: %d rows", reason, count)
    log.info(
        "Kept %d perf_result and %d accuracy_result events; %d named by Buildkite build "
        "(%d commits), the rest by day",
        sum(1 for e in events if e["event"] == "perf_result"),
        sum(1 for e in events if e["event"] == "accuracy_result"),
        named,
        len(builds),
    )

    expected = expected_configs(recipes)
    if expected:
        events.append(
            {
                "event": EXPECTED_CONFIGS_EVENT,
                "received_at": utcnow_iso(),
                "configs": expected,
                "accuracy": expected_accuracy(recipes),
            }
        )

    if dry_run:
        log.info(
            "DRY RUN — nothing written.\n"
            "  perf rows fetched ........ %d\n"
            "  eval rows fetched ........ %d\n"
            "  perf_result events ....... %d\n"
            "  accuracy_result events ... %d",
            len(perf_rows),
            len(eval_rows),
            sum(1 for e in events if e["event"] == "perf_result"),
            sum(1 for e in events if e["event"] == "accuracy_result"),
        )
        return []

    return events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=WINDOW_DAYS,
        help=f"Lookback window in days, 1-{WINDOW_DAYS} (default: {WINDOW_DAYS})",
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Path to write events.jsonl")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query Databricks and report row/event counts without writing anything",
    )
    args = parser.parse_args()

    use_system_certificates()

    for var in ("DATABRICKS_HOST", "DATABRICKS_WAREHOUSE_ID", "DATABRICKS_TOKEN"):
        if not os.getenv(var):
            log.error("%s not set; cannot query Databricks", var)
            return 1

    gh_token = os.getenv("GITHUB_TOKEN") or ""
    events = collect(days=args.days, gh_token=gh_token, dry_run=args.dry_run)
    if args.dry_run:
        return 0

    count = write_events_atomic(Path(args.output), events)
    log.info("Wrote %d events to %s", count, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
