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

``vllm_perf_data_ingest`` rows already carry resolved ``precision``,
``tp``/``ep``/``dp_attention``, and per-GPU metrics pre-converted to
seconds — this collector trusts them directly rather than re-deriving them
from a recipe. ``vllm_eval_data_ingest`` rows carry real Buildkite identity
but no ``model``/``device``; those are recovered from the workload recipe
(fetched at ``main``, the only remaining recipe dependency besides the
coverage card).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perf_eval import WINDOW_DAYS  # noqa: E402
from perf_eval.events import (  # noqa: E402
    EXPECTED_CONFIGS_EVENT,
    write_events_atomic,
)
from perf_eval.normalize import (  # noqa: E402
    is_amd_workload,
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


def perf_event(row: dict) -> dict | None:
    """A canonical ``perf_result`` event from a ``vllm_perf_data_ingest`` row."""
    if not is_nightly_row(row):
        return None
    device = str(row.get("device") or "").strip()
    image = str(row.get("image") or "").strip()
    if not is_amd_workload(image=image, device=device):
        return None
    metrics = perf_metrics(row)
    if not metrics:
        return None
    date = str(row.get("date") or "").strip()
    day = day_bucket(date)
    if not day:
        return None
    return {
        "event": "perf_result",
        "received_at": utcnow_iso(),
        "nightly": True,
        "model": str(row.get("model") or "").strip(),
        "device": device,
        "precision": str(row.get("precision") or "").strip(),
        "parallelism": perf_parallelism(row),
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


def accuracy_event(row: dict, *, recipes: dict[str, tuple[dict, dict]]) -> dict | None:
    """A canonical ``accuracy_result`` event from a ``vllm_eval_data_ingest`` row.

    ``vllm_eval_data_ingest`` rows carry real Buildkite identity but no
    ``model``/``device``; both are recovered from the workload recipe.
    """
    if row.get("kind") != "results" or not is_nightly_row(row):
        return None
    workload = str(row.get("workload") or "").strip()
    task = str(row.get("task") or "").strip()
    image = str(row.get("image") or "").strip()
    recipe = recipes.get(workload)
    entry = recipe[0] if recipe else {}
    device = str(entry.get("device") or "").strip()
    model = str(entry.get("model") or "").strip()
    if not is_amd_workload(workload=workload, image=image, device=device):
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
        return None
    build_number = str(row.get("buildkite_build_number") or "").strip()
    day = day_bucket(str(row.get("_ingest_timestamp") or ""))
    if not day:
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


def _connect():
    import databricks.sql

    host = os.environ["DATABRICKS_HOST"]
    http_path = f"/sql/1.0/warehouses/{os.environ['DATABRICKS_WAREHOUSE_ID']}"
    token = os.environ["DATABRICKS_TOKEN"]
    return databricks.sql.connect(server_hostname=host, http_path=http_path, access_token=token)


def _parse_variant(value):
    """The connector may return a VARIANT column as a dict/list already, or
    as its JSON text; handle both."""
    if isinstance(value, (dict, list)) or value is None:
        return value
    return json.loads(value)


def fetch_rows(conn, table: str, *, since: datetime) -> list[dict]:
    """Every row's parsed ``message``, with the ingest ``timestamp`` folded in
    as ``_ingest_timestamp``. Filtered server-side by ingest time, and
    re-checked client-side since a VARIANT predicate is easy to get subtly
    wrong and this is cheap to double-check.

    VARIANT columns need an explicit ``::`` cast before Databricks will
    compare or order on them (``DATATYPE_MISMATCH.INVALID_ORDERING_TYPE``
    otherwise) — confirmed against the real warehouse.
    """
    cutoff = since.strftime("%Y-%m-%dT%H:%M:%S")
    # Nightly rows only, server-side: the eval table's 30 days of ad-hoc runs
    # run to tens of thousands of rows, and a result that large broke the
    # connector's batch paging ("expected results to start from 0"). The
    # strict client-side is_nightly_row check still applies afterwards.
    query = (
        f"SELECT message, request_metadata:timestamp::string AS ingest_ts "  # noqa: S608
        f"FROM {table} WHERE request_metadata:timestamp::string >= %(cutoff)s "
        f"AND try_cast(message:nightly AS BOOLEAN)"
    )
    with conn.cursor() as cur:
        cur.execute(query, {"cutoff": cutoff})
        rows = cur.fetchall()

    out = []
    for message, ingest_ts in rows:
        row = _parse_variant(message)
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


def collect(*, days: int, gh_token: str, dry_run: bool = False) -> list[dict]:
    if not 1 <= days <= WINDOW_DAYS:
        raise ValueError(f"perf-eval lookback must be between 1 and {WINDOW_DAYS} days")

    since = datetime.now(UTC) - timedelta(days=days)
    conn = _connect()
    try:
        perf_rows = fetch_rows(conn, PERF_TABLE, since=since)
        eval_rows = fetch_rows(conn, EVAL_TABLE, since=since)
    finally:
        conn.close()
    log.info(
        "Fetched %d %s rows and %d %s rows since %s",
        len(perf_rows),
        PERF_TABLE,
        len(eval_rows),
        EVAL_TABLE,
        since.isoformat(),
    )

    recipes = fetch_workload_map(gh_token, ref=RECIPE_REF)

    events: list[dict] = []
    for row in perf_rows:
        event = perf_event(row)
        if event is not None:
            events.append(event)
    for row in eval_rows:
        event = accuracy_event(row, recipes=recipes)
        if event is not None:
            events.append(event)

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
