#!/usr/bin/env python3
"""Fold the event store into the published ``perf_eval.json``.

Scope: AMD only, nightly only, re-applied here instead of trusted from ingest
so a stray event cannot widen what the page shows. Reads only the local store.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perf_eval import (  # noqa: E402
    BUILDKITE_ORG,
    BUILDKITE_PIPELINE_SLUG,
    PIPELINE_URL,
    WINDOW_DAYS,
)
from perf_eval.normalize import (  # noqa: E402
    ACCURACY_BETTER,
    DERIVED_METRICS,
    METRIC_META,
    gpu_count,
    is_amd_workload,
    parallel_key,
    parallel_label,
    parallelism_of,
    score_rows,
    to_int,
)
from perf_eval.store import (  # noqa: E402
    EXPECTED_CONFIGS_EVENT,
    NIGHTLY_RUN_EVENT,
    RESULT_EVENTS,
    finished_at,
    nightly_identity,
    read_events_strict,
    received_at,
    write_json_atomic,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_STORE = ROOT / "data" / "events.jsonl"
DEFAULT_OUTPUT = ROOT / "data" / "perf_eval.json"

# Smallest move that counts as a regression or improvement: 0.5% relative for
# perf, one point absolute for accuracy (0..1 scale). See docs/regression-detection.md.
PERF_REL_THRESHOLD = 0.005
ACCURACY_ABS_THRESHOLD = 0.01


_EPOCH = datetime.min.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Series: one point per nightly
# ---------------------------------------------------------------------------


def _finished_at(result: dict) -> datetime:
    """When a result's nightly finished, sortable.

    Unparseable timestamps sort first rather than crashing the build, but they
    are logged: a silently mis-ordered series is far harder to notice than a
    warning in the collector output.
    """
    parsed = finished_at(result)
    if parsed is None:
        log.warning(
            "perf-eval result has no parseable date; sorting it oldest "
            "(event=%s model=%s build=%s)",
            result.get("event"),
            result.get("model"),
            result.get("build_number"),
        )
        return _EPOCH
    return parsed


def _build_key(event: dict) -> str:
    """How a series point names its build in the payload's ``builds`` table."""
    return str(event.get("build_number"))


def _provenance(event: dict) -> dict:
    """What identifies the build behind a result, published once per build."""
    return {
        "date": event.get("date") or "",
        "nightly_date": event.get("nightly_date") or "",
        "vllm_commit": (event.get("vllm_commit") or "").strip(),
        "build_commit": (event.get("build_commit") or "").strip(),
        "image": (event.get("image") or "").strip(),
        "build_url": event.get("build_url") or "",
    }


def _config_label(device: str, isl, osl, conc) -> str:
    def fmt_len(value):
        if value is None:
            return "?"
        return f"{value // 1024}K" if value and value % 1024 == 0 and value >= 1024 else str(value)

    return f"{fmt_len(isl)} in / {fmt_len(osl)} out @ conc {conc} ({(device or '').upper()})"


def _series_from_points(points: list[dict]) -> list[dict]:
    """One point per nightly, the newest winning by time (the store is not
    time-ordered), sorted oldest first."""
    by_night: dict[str, dict] = {}
    for point in points:
        current = by_night.get(point["nightly_key"])
        if current is None or point["_ts"] >= current["_ts"]:
            by_night[point["nightly_key"]] = point
    return sorted(by_night.values(), key=lambda point: point["_ts"])


def _strip_internal(series: list[dict]) -> list[dict]:
    return [
        {k: v for k, v in point.items() if not k.startswith("_") and k != "nightly_key"}
        for point in series
    ]


def _parallel_fields(parallelism: dict) -> dict:
    """The page keys configs and labels them on ``parallel_label``."""
    return {
        "parallelism": parallelism,
        "parallel_label": parallel_label(parallelism),
        "gpus": gpu_count(parallelism),
    }


def build_perf_configs(perf_events: list[dict]) -> list[dict]:
    """Per-config metric series, a config being device, parallelism, precision and shape."""
    configs: dict[tuple, dict] = {}
    for event in perf_events:
        device = (event.get("device") or "").strip()
        isl, osl, conc = event.get("isl"), event.get("osl"), event.get("conc")
        parallelism, precision = parallelism_of(event), event.get("precision") or ""
        key = (device, parallel_key(parallelism), precision, isl, osl, conc)
        config = configs.setdefault(
            key,
            {
                "device": device,
                "isl": isl,
                "osl": osl,
                "conc": conc,
                **_parallel_fields(parallelism),
                "precision": precision,
                "label": _config_label(device, isl, osl, conc),
                "_metric_points": {},
                "_jobs": {},
            },
        )
        # The Buildkite job that ran this config, so a link can open it.
        if event.get("buildkite_artifact_job_id"):
            config["_jobs"][_build_key(event)] = event["buildkite_artifact_job_id"]
        timestamp = _finished_at(event)
        night = nightly_identity(event)
        for metric, value in (event.get("metrics") or {}).items():
            config["_metric_points"].setdefault(metric, []).append(
                {
                    "nightly_key": night,
                    "_ts": timestamp,
                    "build": _build_key(event),
                    "value": value,
                    "completed_requests": event.get("completed_requests"),
                    "failed_requests": event.get("failed_requests"),
                }
            )

    out = []
    for config in configs.values():
        metric_points = config.pop("_metric_points")
        config["jobs"] = config.pop("_jobs")
        metrics_out = {}
        for metric, points in metric_points.items():
            meta = METRIC_META.get(metric, {"better": "higher"})
            # The page judges latest-vs-previous itself: the window, not this
            # series, decides which two nightlies are compared.
            metrics_out[metric] = {
                "label": meta.get("label", metric),
                "unit": meta.get("unit", ""),
                "better": meta["better"],
                "series": _strip_internal(_series_from_points(points)),
            }
        config["metrics"] = metrics_out
        out.append(config)
    # Stable, human-friendly ordering: device, then concurrency, then ISL/OSL.
    out.sort(
        key=lambda c: (
            c["device"],
            c.get("conc") or 0,
            c.get("isl") or 0,
            c.get("osl") or 0,
            c["gpus"],
            c["parallel_label"],
            c["precision"],
        )
    )
    return out


def build_accuracy_tasks(eval_events: list[dict]) -> list[dict]:
    """Per-(workload, device, task, metric) accuracy series."""
    tasks: dict[tuple, dict] = {}
    for event in eval_events:
        timestamp = _finished_at(event)
        night = nightly_identity(event)
        device = (event.get("device") or "").strip()
        workload = (event.get("workload") or "").strip()
        for row in score_rows(event.get("results") or []):
            key = (workload, device, row["task"], row["metric"])
            entry = tasks.setdefault(
                key,
                {
                    "workload": workload,
                    "device": device,
                    "task": row["task"],
                    "metric": row["metric"],
                    "primary": bool(row.get("primary")),
                    "_points": [],
                    "jobs": {},
                },
            )
            if event.get("buildkite_artifact_job_id"):
                entry["jobs"][_build_key(event)] = event["buildkite_artifact_job_id"]
            entry["primary"] = entry["primary"] or bool(row.get("primary"))
            entry["_points"].append(
                {
                    "nightly_key": night,
                    "_ts": timestamp,
                    "build": _build_key(event),
                    "value": row["value"],
                }
            )

    out = []
    for entry in tasks.values():
        entry["series"] = _strip_internal(_series_from_points(entry.pop("_points")))
        out.append(entry)
    out.sort(key=lambda t: (not t["primary"], t["device"], t["workload"], t["task"], t["metric"]))
    return out


# ---------------------------------------------------------------------------
# Coverage: what the recipes expect
# ---------------------------------------------------------------------------


def _expected_from_events(events: list[dict]) -> dict:
    """The newest expected-configs snapshot, for the coverage card."""
    newest: dict | None = None
    newest_at: datetime | None = None
    for event in events:
        if event.get("event") != EXPECTED_CONFIGS_EVENT:
            continue
        observed_at = received_at(event) or _EPOCH
        if newest_at is None or observed_at >= newest_at:
            newest, newest_at = event, observed_at
    if newest is None:
        return {"recorded_at": "", "configs": [], "accuracy": []}
    configs = [
        {**config, **_parallel_fields(parallelism_of(config))}
        for config in newest.get("configs") or []
        if isinstance(config, dict)
    ]
    # Snapshots taken before accuracy was recipe-derived have no `accuracy`.
    accuracy = [task for task in newest.get("accuracy") or [] if isinstance(task, dict)]
    return {
        "recorded_at": newest.get("received_at") or "",
        "configs": configs,
        "accuracy": accuracy,
    }


def _tp_shape(record: dict, tp: int) -> tuple:
    return (
        (record.get("model") or "").strip(),
        (record.get("device") or "").strip(),
        record.get("precision") or "",
        tp,
        record.get("isl"),
        record.get("osl"),
        record.get("conc"),
    )


def _recipe_parallelism(expected_configs: list[dict]) -> dict[tuple, dict]:
    """The recipes' parallelism, keyed on what a perf event stored before the
    parallelism map existed does carry: TP and the shape.

    Without it, those events of an expert-parallel recipe would sit on a line of
    their own. A shape two recipes share, differing only past TP, is left alone.
    """
    options: dict[tuple, list[dict]] = {}
    for config in expected_configs:
        if isinstance(config.get("parallelism"), dict):
            parallelism = config["parallelism"]
            tp = parallelism.get("tensor_parallel_size", 1)
            options.setdefault(_tp_shape(config, tp), []).append(parallelism)
    return {
        shape: found[0]
        for shape, found in options.items()
        if len({parallel_key(parallelism) for parallelism in found}) == 1
    }


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def _nightly_runs(events: list[dict]) -> list[dict]:
    """Every nightly build the collector saw, newest first, with how many AMD
    results each produced: zero means it failed or ran no AMD workload."""
    results: dict[str, int] = {}
    for event in events:
        if _is_in_scope(event):
            key = _build_key(event)
            results[key] = results.get(key, 0) + 1
    runs: dict[str, dict] = {}
    for event in events:
        if event.get("event") != NIGHTLY_RUN_EVENT:
            continue
        key = _build_key(event)
        if key not in runs or (received_at(event) or _EPOCH) >= (received_at(runs[key]) or _EPOCH):
            runs[key] = event
    published = [
        {
            "build": key,
            "nightly_date": run.get("nightly_date") or "",
            "date": run.get("date") or "",
            "state": run.get("state") or "",
            "build_url": run.get("build_url") or "",
            "amd_results": results.get(key, 0),
            # AMD workloads a nightly still going has left to run.
            "amd_pending": run.get("amd_pending") or [],
        }
        for key, run in runs.items()
    ]
    published.sort(key=lambda r: (r["nightly_date"] or r["date"][:10], to_int(r["build"]) or 0))
    return published[::-1]


def _is_in_scope(event: dict) -> bool:
    """The AMD-only, nightly-only scope filter, re-applied at aggregation."""
    if event.get("event") not in RESULT_EVENTS:
        return False
    if event.get("nightly") is not True:
        return False
    return is_amd_workload(
        workload=event.get("workload"),
        image=event.get("image"),
        device=event.get("device"),
    )


def aggregate(events: list[dict]) -> dict:
    """Fold the event log into the frontend payload (AMD + nightly only)."""
    perf_by_model: dict[str, list[dict]] = {}
    eval_by_model: dict[str, list[dict]] = {}
    devices: set[str] = set()
    nightlies: set[str] = set()
    expected = _expected_from_events(events)
    recipe_parallelism = _recipe_parallelism(expected["configs"])

    # Each build's provenance, once; the newest event of a build wins.
    builds: dict[str, tuple[datetime, dict]] = {}
    for event in events:
        if not _is_in_scope(event):
            continue
        when = _finished_at(event)
        key = _build_key(event)
        if key not in builds or when >= builds[key][0]:
            builds[key] = (when, _provenance(event))
        model = (event.get("model") or "").strip() or "(unknown model)"
        if event["event"] == "perf_result":
            if "parallelism" not in event:
                shape = _tp_shape(event, to_int(event.get("tp")) or 1)
                if shape in recipe_parallelism:
                    event = {**event, "parallelism": recipe_parallelism[shape]}
            perf_by_model.setdefault(model, []).append(event)
        elif event["event"] == "accuracy_result":
            eval_by_model.setdefault(model, []).append(event)
        if event.get("device"):
            devices.add(event["device"])
        nightlies.add(nightly_identity(event))

    models = []
    perf_points = accuracy_points = 0
    for model in sorted(set(perf_by_model) | set(eval_by_model)):
        perf_events = perf_by_model.get(model, [])
        eval_events = eval_by_model.get(model, [])
        perf_configs = build_perf_configs(perf_events)
        accuracy_tasks = build_accuracy_tasks(eval_events)
        perf_points += sum(len(m["series"]) for c in perf_configs for m in c["metrics"].values())
        accuracy_points += sum(len(t["series"]) for t in accuracy_tasks)
        models.append(
            {
                "model": model,
                "perf_configs": perf_configs,
                "accuracy_tasks": accuracy_tasks,
            }
        )

    # Only the builds a published point names: a retried nightly's other build
    # has none.
    referenced = {
        point["build"]
        for model in models
        for series in [
            *(m["series"] for c in model["perf_configs"] for m in c["metrics"].values()),
            *(t["series"] for t in model["accuracy_tasks"]),
        ]
        for point in series
    }

    # The published JSON is key-sorted, so insertion order cannot survive the
    # round trip. Publish the display order explicitly instead.
    metric_meta = {
        key: {**meta, "order": order, "counted": key not in DERIVED_METRICS}
        for order, (key, meta) in enumerate(METRIC_META.items())
    }
    metric_meta["accuracy"] = {
        "label": "Accuracy",
        "unit": "",
        "better": ACCURACY_BETTER,
        "digits": 4,
        "order": len(METRIC_META),
    }

    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": {
            "hardware": "amd",
            "runs": "nightly",
            "description": (
                "AMD (MI-series) workloads from scheduled nightly builds of the "
                "vllm/perf-eval pipeline. NVIDIA workloads and ad-hoc builds are "
                "deliberately excluded."
            ),
        },
        "pipeline": {
            "org": BUILDKITE_ORG,
            "slug": BUILDKITE_PIPELINE_SLUG,
            "url": PIPELINE_URL,
        },
        "metric_meta": metric_meta,
        "thresholds": {
            "perf_rel": PERF_REL_THRESHOLD,
            "accuracy_abs": ACCURACY_ABS_THRESHOLD,
        },
        "expected": expected,
        # Series points name a build; its date and provenance are here once.
        "builds": {key: builds[key][1] for key in sorted(referenced)},
        "nightly_runs": _nightly_runs(events),
        "models": models,
        "summary": {
            "models": len(models),
            "amd_devices": sorted(devices),
            "nightlies": len(nightlies),
            "perf_points": perf_points,
            "accuracy_points": accuracy_points,
        },
    }


def build_payload(events: list[dict]) -> dict:
    """The published payload: the nightlies from the last WINDOW_DAYS.

    A nightly is in if its latest result is inside the window, so a nightly
    is never split across the edge.
    """
    window_start = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
    latest: dict[str, datetime] = {}
    for event in events:
        if _is_in_scope(event):
            identity = nightly_identity(event)
            latest[identity] = max(latest.get(identity, _EPOCH), _finished_at(event))
    published = {identity for identity, last in latest.items() if last >= window_start}

    payload = aggregate(
        [e for e in events if not _is_in_scope(e) or nightly_identity(e) in published]
    )
    payload["retention"] = {"display_window_days": WINDOW_DAYS}
    return payload


def payload_problems(payload: dict) -> list[str]:
    """Why a payload is unfit to publish; empty if it is fine."""
    required = (
        "generated_at",
        "models",
        "builds",
        "nightly_runs",
        "summary",
        "thresholds",
        "metric_meta",
        "expected",
    )
    problems = [f"missing {key}" for key in required if key not in payload]
    if not isinstance(payload.get("models"), list):
        problems.append("models is not a list")
    elif (payload.get("summary") or {}).get("perf_points") and not payload["models"]:
        problems.append("perf points counted but no models published")
    else:
        builds = payload.get("builds") or {}
        named = {
            point.get("build")
            for model in payload["models"]
            for series in [
                *(m["series"] for c in model["perf_configs"] for m in c["metrics"].values()),
                *(t["series"] for t in model["accuracy_tasks"]),
            ]
            for point in series
        }
        # A result without a build number would publish as build "None".
        unlisted = sorted(str(b) for b in named if b not in builds or b == "None")
        if unlisted:
            problems.append(f"points name builds with no provenance: {unlisted}")
    return problems


def summary_markdown(payload: dict | None, *, deploy_skipped: bool = False) -> str:
    """The run summary shown on the workflow run's page."""
    lines = ["### Perf Eval collection", ""]
    if deploy_skipped:
        lines.append("- Deploy skipped: no new results since the last publish.")
    if payload is None:
        lines.append("- No payload was produced.")
    else:
        summary = payload.get("summary") or {}
        lines += [
            f"- Generated at: `{payload.get('generated_at')}`",
            f"- Models: {summary.get('models')}",
            f"- Nightlies: {summary.get('nightlies')}",
            f"- Perf points: {summary.get('perf_points')}",
            f"- Accuracy points: {summary.get('accuracy_points')}",
            f"- AMD hardware: {', '.join(summary.get('amd_devices') or []) or 'none'}",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="Path to events.jsonl")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Path to perf_eval.json")
    parser.add_argument(
        "--summarize",
        metavar="PAYLOAD",
        help="Instead of aggregating, print a Markdown summary of an existing payload",
    )
    parser.add_argument(
        "--deploy-skipped",
        action="store_true",
        help="With --summarize, note that the deploy was skipped",
    )
    args = parser.parse_args()

    if args.summarize:
        path = Path(args.summarize)
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        print(summary_markdown(payload, deploy_skipped=args.deploy_skipped), end="")
        return 0

    events = read_events_strict(Path(args.store))
    payload = build_payload(events)
    problems = payload_problems(payload)
    if problems:
        log.error("Payload failed sanity checks, not writing it: %s", "; ".join(problems))
        return 1
    out = Path(args.output)
    write_json_atomic(out, payload)
    log.info(
        "Wrote %s: %d models, %d nightlies, %d perf points, %d accuracy points",
        out,
        payload["summary"]["models"],
        payload["summary"]["nightlies"],
        payload["summary"]["perf_points"],
        payload["summary"]["accuracy_points"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
