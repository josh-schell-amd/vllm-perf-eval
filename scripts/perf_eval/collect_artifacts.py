#!/usr/bin/env python3
"""Collect AMD nightly perf-eval results from Buildkite artifacts into the store.

Scope: AMD workloads (``is_amd_workload``) in nightly builds (``is_nightly_build``).
Lists finished nightly builds on main, downloads each AMD workload's
``bench-*.json`` (perf) and ``results_*.json`` (accuracy), labels them from the
recipes at the build's perf-eval commit, and appends the new ones. Every
Buildkite and GitHub request is a GET.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import tarfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perf_eval import (  # noqa: E402
    BUILDKITE_API_BASE,
    BUILDKITE_ORG,
    BUILDKITE_PIPELINE_SLUG,
    WINDOW_DAYS,
    WORKLOAD_REPO,
)
from perf_eval.normalize import (  # noqa: E402
    gpu_count,
    is_amd_workload,
    normalize_eval_payload,
    parallelism_of,
    to_float,
    to_int,
    transform_perf,
    utcnow_iso,
)
from perf_eval.store import (  # noqa: E402
    ARTIFACT_MARKER_EVENT,
    EXPECTED_CONFIGS_EVENT,
    NIGHTLY_RUN_EVENT,
    RESULT_EVENTS,
    append_events,
    artifact_key,
    artifact_keys_from_event,
    compact_events,
    event_key,
    finished_at,
    read_events_strict,
    received_at,
    write_events_atomic,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_STORE = ROOT / "data" / "events.jsonl"


# Running out of retries is an error: an empty page would pass for a complete listing.
BK_GET_MAX_ATTEMPTS = 3
BK_GET_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504, 520, 522, 524})
BK_GET_RETRY_BACKOFF_SECONDS = 2
# Buildkite's rate limit is per organization, shared with everyone using
# vllm's API: pause for the reset before using the last of it.
BK_RATE_LIMIT_RESERVE = 20
# Longest wait a rate-limit header can ask for; the window is a minute.
BK_RATE_LIMIT_MAX_WAIT_SECONDS = 70

# "Nightly run 2026-06-30: commit 93d8f834dd8acf33eb0e2a75b2711b628cb6e226"
# Parsed for the date and vLLM commit once a build has been accepted.
_NIGHTLY_MSG_RE = re.compile(
    r"nightly\s+run\s+(\d{4}-\d{2}-\d{2}).*?commit\s+([0-9a-f]{7,40})",
    re.IGNORECASE | re.DOTALL,
)

# ``results/<workload>/bench-<run>.json`` (perf) and
# ``results/<workload>/<task>/[<model>/]results_*.json`` (accuracy; lm-eval adds <model>).
_PERF_ARTIFACT_RE = re.compile(r"^results/(?P<wl>[^/]+)/bench-(?P<cfg>.+)\.json$")
_ACC_ARTIFACT_RE = re.compile(
    r"^results/(?P<wl>[^/]+)/(?P<task>[^/]+)/(?:[^/]+/)?results_[^/]*\.json$"
)
# Buildkite path filters: just the result files, not the much larger sample/log tree.
_RESULT_ARTIFACT_PATHS = (
    "*results/*/bench-*.json",
    "*results/*/*/results_*.json",
    "*results/*/*/*/results_*.json",
)
_RESULT_ARTIFACT_MAX_PAGES = 10

# Result events retain the exact Buildkite artifact that produced them.
_ARTIFACT_PROVENANCE_FIELDS = (
    "buildkite_artifact_id",
    "buildkite_artifact_job_id",
    "buildkite_artifact_path",
    "buildkite_artifact_sha1",
)

# Ingested builds are not re-listed, except the newest few: a retried job can
# add artifacts to a finished build.
DEFAULT_RECHECK_BUILDS = 3
# The window holds about 30 nightlies; 60 caps a backfill at 1 + 3x60 listings.
MAX_RECHECK_BUILDS = 60

# Enough for a cold start over the full window; a logic error stops here.
DEFAULT_MAX_REQUESTS = 3000


class RequestBudget:
    """Counts Buildkite requests, retries included, and raises past a ceiling."""

    def __init__(self, max_requests: int | None = DEFAULT_MAX_REQUESTS):
        self.listings = 0
        self.downloads = 0
        self.skipped_builds = 0
        self.max_requests = max_requests

    @property
    def total(self) -> int:
        return self.listings + self.downloads

    def charge(self, kind: str) -> None:
        if kind == "download":
            self.downloads += 1
        else:
            self.listings += 1
        if self.max_requests is not None and self.total > self.max_requests:
            raise RuntimeError(
                f"perf-eval Buildkite request ceiling exceeded: {self.total} > "
                f"{self.max_requests}. Re-run with a smaller --days, or raise "
                "--max-requests deliberately if a backfill genuinely needs it."
            )

    def summary(self) -> str:
        return (
            f"{self.total} Buildkite requests "
            f"({self.listings} listings, {self.downloads} downloads)"
        )


# ---------------------------------------------------------------------------
# Pure helpers (no I/O) — unit tested without a live Buildkite/GitHub
# ---------------------------------------------------------------------------


def use_system_certificates() -> bool:
    """Verify TLS against the OS trust store instead of certifi's bundle.

    Needed behind proxies that re-sign TLS with a CA only the OS trusts.
    Returns False if truststore is not installed.
    """
    try:
        import truststore
    except ModuleNotFoundError:
        log.debug("truststore not installed; using certifi's CA bundle")
        return False
    truststore.inject_into_ssl()
    return True


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


# vLLM's short forms, plus the --tp/--dp spellings perf-eval's parse_tp accepts.
_PARALLEL_ALIASES = {
    "-tp": "--tensor-parallel-size",
    "--tp": "--tensor-parallel-size",
    "-pp": "--pipeline-parallel-size",
    "-dp": "--data-parallel-size",
    "--dp": "--data-parallel-size",
    "-dcp": "--decode-context-parallel-size",
    "-pcp": "--prefill-context-parallel-size",
}
# Where and how data-parallel ranks start, not what the server runs.
_PARALLEL_LAUNCH_FLAGS = frozenset(
    {
        "--data-parallel-rank",
        "--data-parallel-start-rank",
        "--data-parallel-size-local",
        "--data-parallel-address",
        "--data-parallel-rpc-port",
        "--data-parallel-backend",
        "--data-parallel-hybrid-lb",
        "--data-parallel-external-lb",
        "--data-parallel-multi-port-external-lb",
        "--max-parallel-loading-workers",
    }
)


def _options(args: str):
    """(flag, value) per option on a command line; value is None for a bare switch."""
    tokens = (args or "").split()
    for index, token in enumerate(tokens):
        if not token.startswith("-"):
            continue
        flag, has_value, value = token.partition("=")
        if has_value:
            yield flag, value
        elif index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
            yield flag, tokens[index + 1]
        else:
            yield flag, None


def parse_parallelism(serve_args: str) -> dict:
    """Every ``--*parallel*`` flag in serve_args, by vLLM's name, defaults left out."""
    found: dict = {}
    for flag, value in _options(serve_args):
        flag = _PARALLEL_ALIASES.get(flag, flag)
        switched_off = flag.startswith("--no-")
        if switched_off:
            flag = "--" + flag.removeprefix("--no-")
        if not flag.startswith("--") or "parallel" not in flag or flag in _PARALLEL_LAUNCH_FLAGS:
            continue
        name = flag.removeprefix("--").replace("-", "_")
        if name.endswith("_size"):
            size = to_int(value)
            if size not in (None, 1):
                found[name] = size
        elif value is not None:
            found[name] = value
        elif not switched_off:
            found[name] = True
    return found


def precision_from_model(model: str) -> str:
    """The precision the model id names, or "" if it names none."""
    name = (model or "").lower()
    for marker in ("fp4", "fp8", "int4", "int8", "bf16", "fp16"):
        if marker in name:
            return marker
    return ""


# torch dtype names as checkpoint configs and --dtype spell them.
_DTYPE_LABELS = {
    "bfloat16": "bf16",
    "bf16": "bf16",
    "float16": "fp16",
    "half": "fp16",
    "fp16": "fp16",
}


def _quant_label(method: str) -> str:
    """A vLLM --quantization method as a precision: its fp8/fp4/int4 part
    when it names one (``deepseek_v4_fp8`` is fp8), else the method."""
    method = (method or "").strip().lower()
    for marker in ("mxfp4", "nvfp4", "fp4", "fp8", "int4", "int8"):
        if marker in method:
            return marker
    return method


def checkpoint_precision(config: dict) -> tuple[str, str]:
    """What a Hugging Face checkpoint's config.json says the weights are, as
    (quantized format, dtype). Either is "" when the config does not say.

    vLLM loads quantized weights in the format ``quantization_config``
    declares, and otherwise in ``dtype`` (``torch_dtype`` before
    transformers 4.56). A multimodal checkpoint keeps both in ``text_config``.
    """
    configs = [config, config.get("text_config") or {}]
    quant = next((c["quantization_config"] for c in configs if c.get("quantization_config")), {})
    dtype = next(
        (
            c.get("dtype") or c.get("torch_dtype")
            for c in configs
            if c.get("dtype") or c.get("torch_dtype")
        ),
        "",
    )
    quantized = ""
    if isinstance(quant, dict) and quant:
        method = str(quant.get("quant_method") or "").lower()
        if method == "compressed-tensors":
            # The format names the scheme (int-quantized, float-quantized,
            # nvfp4-pack-quantized ...); config_groups give the weight width.
            fmt = str(quant.get("format") or "").lower()
            weights = next(
                (
                    g.get("weights") or {}
                    for g in (quant.get("config_groups") or {}).values()
                    if isinstance(g, dict)
                ),
                {},
            )
            bits, kind = weights.get("num_bits"), str(weights.get("type") or "").lower()
            if "nvfp4" in fmt or "mxfp4" in fmt:
                quantized = "nvfp4" if "nvfp4" in fmt else "mxfp4"
            elif bits and kind in ("int", "float"):
                quantized = ("int" if kind == "int" else "fp") + str(bits)
            else:
                quantized = method
        elif method == "quark":
            # AMD Quark: the weight spec gives the element type; fp4 in groups
            # with an e8m0 scale is the OCP MX format, mxfp4.
            weight = (quant.get("global_quant_config") or {}).get("weight") or {}
            element = str(weight.get("dtype") or "").lower()
            if "fp4" in element and str(weight.get("scale_format") or "").lower() == "e8m0":
                quantized = "mxfp4"
            else:
                quantized = _quant_label(element) or method
        else:
            quantized = _quant_label(method)
    return quantized, _DTYPE_LABELS.get(str(dtype).lower(), "")


def recipe_precision(meta: dict, serve_args: str, model: str, checkpoint: dict | None) -> str:
    """The precision a recipe runs at, from what decides it, first match wins:
    the recipe's own ``precision``; ``--quantization``; the checkpoint's
    quantization_config; ``--dtype``; the checkpoint's dtype; a marker in the
    model id. "" only when none of them says."""
    if meta.get("precision"):
        return str(meta["precision"]).strip()
    options = dict(_options(serve_args))
    quantization = options.get("--quantization") or options.get("-q")
    if quantization:
        return _quant_label(quantization)
    quantized, dtype = checkpoint_precision(checkpoint or {})
    if quantized:
        return quantized
    flag_dtype = _DTYPE_LABELS.get(str(options.get("--dtype") or "").lower(), "")
    return flag_dtype or dtype or precision_from_model(model)


def lm_eval_tasks(data: dict) -> list[str]:
    """The lm-eval task names a recipe declares, sorted."""
    tasks = (data.get("lm_eval") or {}).get("tasks") or []
    names = {(task.get("name") or "").strip() for task in tasks if isinstance(task, dict)}
    return sorted(names - {""})


def workload_entry(data: dict, checkpoint: dict | None = None) -> tuple[dict, dict]:
    """A recipe as (entry, configs by run name).

    Runs are named ``<config>-conc-<concurrency>``, as perf-eval expands them
    and names their artifacts (``bench-<run>.json``).
    """
    gpu = (data.get("gpu") or "").strip()
    vllm = data.get("vllm") or {}
    bench = data.get("vllm_bench") or {}
    meta = bench.get("metadata") or {}
    model = (vllm.get("model") or "").strip()
    serve_args = vllm.get("serve_args") or ""
    configs = {}
    for config in bench.get("configs") or []:
        name = config.get("name")
        if not name:
            continue
        raw_concurrency = config.get("max_concurrency")
        # A sweep is a list; a single value still expands to one suffixed run.
        concurrencies = raw_concurrency if isinstance(raw_concurrency, list) else [raw_concurrency]
        for concurrency in concurrencies:
            configs[f"{name}-conc-{concurrency}"] = {
                "isl": config.get("input_len"),
                "osl": config.get("output_len"),
                "conc": concurrency,
            }
    parallelism = parse_parallelism(serve_args)
    # perf-eval's escape hatch for a TP that serve_args does not show.
    meta_tp = to_int(meta.get("tp"))
    if meta_tp is not None:
        parallelism.pop("tensor_parallel_size", None)
        if meta_tp != 1:
            parallelism["tensor_parallel_size"] = meta_tp
    entry = {
        "name": (data.get("name") or "").strip(),
        "gpu": gpu,
        "device": (meta.get("device") or gpu.lower()).strip(),
        "parallelism": parallelism,
        "precision": recipe_precision(meta, serve_args, model, checkpoint),
        "model": model,
        # Only scheduled nightlies are in scope, so only they are expected.
        "nightly": data.get("nightly") is True,
        "accuracy_tasks": lm_eval_tasks(data),
    }
    return entry, configs


def _expected_workloads(workloads: dict[str, tuple[dict, dict]]):
    """The AMD nightly recipes, in workload order: the scope both expectations share."""
    for workload, (entry, configs) in sorted(workloads.items()):
        if not entry.get("nightly"):
            continue
        if not is_amd_workload(workload=workload, device=entry.get("device")):
            continue
        yield workload, entry, configs


def expected_accuracy(workloads: dict[str, tuple[dict, dict]]) -> list[dict]:
    """The AMD nightly accuracy results the recipes say should run.

    From the recipes, not past results, so a long outage still shows as missing.
    """
    out: list[dict] = []
    for workload, entry, _ in _expected_workloads(workloads):
        for task in entry.get("accuracy_tasks") or []:
            out.append(
                {
                    "workload": workload,
                    "model": entry.get("model") or "",
                    "device": entry.get("device") or "",
                    "task": task,
                }
            )
    return out


def expected_configs(workloads: dict[str, tuple[dict, dict]]) -> list[dict]:
    """The AMD nightly configs the recipes say should run, for the coverage card."""
    out: list[dict] = []
    for workload, entry, configs in _expected_workloads(workloads):
        for run_name, config in sorted(configs.items()):
            out.append(
                {
                    "workload": workload,
                    "run": run_name,
                    "model": entry.get("model") or "",
                    "device": entry.get("device") or "",
                    "precision": entry.get("precision") or "",
                    "parallelism": parallelism_of(entry),
                    "isl": config.get("isl"),
                    "osl": config.get("osl"),
                    "conc": config.get("conc"),
                }
            )
    return out


def is_nightly_build(build: dict) -> bool:
    """Whether a build is a scheduled nightly.

    The nightly trigger sets ``NIGHTLY=1`` on a ``main`` build, and that env
    var is the only signal.
    """
    branch = (build.get("branch") or "").strip()
    env = build.get("env") or {}
    return branch in {"main", "master"} and _truthy(env.get("NIGHTLY"))


def nightly_info(build: dict) -> dict | None:
    """``{vllm_commit, branch, nightly_date}`` for a nightly build, else None.

    ``nightly_date`` is the date Buildkite names the nightly by ("Nightly run
    2026-09-26"), which can differ from the day it finished; "" if the
    message names none.
    """
    if not is_nightly_build(build):
        return None

    branch = (build.get("branch") or "").strip()
    env = build.get("env") or {}
    match = _NIGHTLY_MSG_RE.search(build.get("message") or "")
    commit = match.group(2) if match else None
    if not commit:
        commit = (env.get("VLLM_COMMIT") or "").strip()
    return {
        "vllm_commit": commit,
        "branch": branch or "main",
        "nightly_date": match.group(1) if match else "",
    }


def nightly_run(build: dict, night: dict) -> dict:
    """A record that a nightly build ran, whatever it produced."""
    return {
        "event": NIGHTLY_RUN_EVENT,
        "received_at": utcnow_iso(),
        "build_number": build.get("number"),
        "nightly_date": night["nightly_date"],
        "date": build.get("finished_at") or "",
        "state": build.get("state") or "",
        "build_url": build.get("web_url") or "",
        "vllm_commit": night["vllm_commit"],
    }


def job_image(env: dict) -> str:
    """The container image a job ran in, from its Kubernetes plugin, or "".

    Not the build's VLLM_IMAGE: that is the CUDA image, which perf-eval does
    not use for ROCm jobs, and a recipe with ``pin_image`` runs its own.
    """
    try:
        plugins = json.loads(env.get("BUILDKITE_PLUGINS") or "[]")
    except ValueError:
        return ""
    images: set[str] = set()
    for plugin in plugins if isinstance(plugins, list) else []:
        for config in plugin.values() if isinstance(plugin, dict) else []:
            pod = (config.get("podSpecPatch") or {}) if isinstance(config, dict) else {}
            images.update(str(c["image"]) for c in pod.get("containers") or [] if c.get("image"))
    # More than one would be a guess.
    return images.pop() if len(images) == 1 else ""


def classify_artifact(path: str) -> tuple[str, str, str] | None:
    """``("perf", workload, run)``, ``("accuracy", workload, task)``, or None."""
    norm = (path or "").strip().lstrip("./")
    match = _PERF_ARTIFACT_RE.match(norm)
    if match:
        return "perf", match.group("wl"), match.group("cfg")
    match = _ACC_ARTIFACT_RE.match(norm)
    if match:
        return "accuracy", match.group("wl"), match.group("task")
    return None


def perf_event(
    raw: dict,
    *,
    entry: dict,
    config: dict,
    identity: dict,
) -> dict | None:
    """Build a canonical ``perf_result`` event from a raw bench artifact."""
    device = entry.get("device") or ""
    image = identity.get("image") or ""
    if not is_amd_workload(image=image, device=device, workload=entry.get("name")):
        return None
    # A crashed benchmark still writes a bench json; zero throughput would read
    # as a 100% regression.
    total = to_float(raw.get("total_token_throughput"))
    if total is None or total <= 0:
        log.warning(
            "Skipping bench result with no positive total_token_throughput (workload=%s build=%s)",
            entry.get("name"),
            identity.get("build_number"),
        )
        return None
    parallelism = parallelism_of(entry)
    metrics = transform_perf(raw, gpus=gpu_count(parallelism))
    if not metrics:
        return None
    conc = config.get("conc")
    if conc is None:
        conc = raw.get("max_concurrency")
    return {
        "event": "perf_result",
        "received_at": utcnow_iso(),
        "nightly": True,
        "model": (raw.get("model_id") or entry.get("model") or "").strip(),
        "device": device,
        "precision": entry.get("precision") or "",
        "parallelism": parallelism,
        "isl": config.get("isl"),
        "osl": config.get("osl"),
        "conc": conc,
        "date": identity.get("date") or "",
        "nightly_date": identity.get("nightly_date") or "",
        "build_number": identity.get("build_number"),
        "build_url": identity.get("build_url") or "",
        "build_commit": identity.get("build_commit") or "",
        "branch": identity.get("branch") or "main",
        "image": image,
        "vllm_commit": identity.get("vllm_commit") or "",
        # Floats: perf-eval takes the median across repetitions.
        "completed_requests": to_float(raw.get("completed")),
        "failed_requests": to_float(raw.get("failed")),
        "metrics": metrics,
    }


def accuracy_event(
    results_json: dict,
    *,
    workload: str,
    task: str,
    entry: dict,
    identity: dict,
) -> dict | None:
    """Build a canonical ``accuracy_result`` event from an lm-eval artifact."""
    payload = {
        "kind": "results",
        # The recipe's model, not lm-eval's: lm-eval records its client backend
        # (``local-completions``), and this is the string perf results carry.
        "model": entry.get("model") or "",
        "workload": workload,
        "task": task,
        "device": entry.get("device") or "",
        "image": identity.get("image") or "",
        "vllm_commit": identity.get("vllm_commit") or "",
        "buildkite_build_number": identity.get("build_number"),
        "buildkite_build_url": identity.get("build_url") or "",
        "buildkite_commit": identity.get("build_commit") or "",
        "buildkite_branch": identity.get("branch") or "main",
        "nightly": True,
        "data": results_json,
    }
    event = normalize_eval_payload(payload)
    if event is None:
        return None
    event["nightly"] = True
    event["date"] = identity.get("date") or ""
    event["nightly_date"] = identity.get("nightly_date") or ""
    return event


def artifact_provenance(artifact: dict, build_number: Any) -> dict:
    """The stable fields identifying an artifact; never its signed download URL."""
    return {
        "build_number": build_number,
        "buildkite_artifact_id": str(artifact.get("id") or "").strip(),
        "buildkite_artifact_job_id": str(artifact.get("job_id") or "").strip(),
        "buildkite_artifact_path": str(artifact.get("path") or "").strip().lstrip("./"),
        "buildkite_artifact_sha1": str(artifact.get("sha1sum") or "").strip().lower(),
    }


def artifact_marker(provenance: dict) -> dict:
    """Build a marker that compaction folds into the exact artifact index."""
    return {
        "event": ARTIFACT_MARKER_EVENT,
        "received_at": utcnow_iso(),
        **provenance,
    }


# ---------------------------------------------------------------------------
# I/O — Buildkite REST + GitHub raw (read-only)
# ---------------------------------------------------------------------------


def _header_seconds(headers, name: str) -> int | None:
    try:
        return max(0, int(float(headers.get(name, ""))))
    except (TypeError, ValueError):
        return None


def _retry_wait(resp, attempt: int) -> int:
    """How long to wait before retrying: until the rate limit resets when
    Buildkite says when, else a short backoff."""
    reset = _header_seconds(resp.headers, "RateLimit-Reset")
    if reset is None:
        reset = _header_seconds(resp.headers, "Retry-After")
    if reset is None:
        return BK_GET_RETRY_BACKOFF_SECONDS * attempt
    return min(reset + 1, BK_RATE_LIMIT_MAX_WAIT_SECONDS)


def _pace(resp) -> None:
    """Wait for the rate limit to reset when little of it is left. A download
    redirects to storage, so Buildkite's headers are on the first response."""
    first = resp.history[0] if getattr(resp, "history", None) else resp
    remaining = _header_seconds(first.headers, "RateLimit-Remaining")
    reset = _header_seconds(first.headers, "RateLimit-Reset")
    if remaining is not None and reset is not None and remaining < BK_RATE_LIMIT_RESERVE:
        wait = min(reset + 1, BK_RATE_LIMIT_MAX_WAIT_SECONDS)
        log.info("Buildkite rate limit nearly used (%d left); waiting %ds", remaining, wait)
        time.sleep(wait)


def _bk_get(path: str, token: str, params: dict | None = None, budget: RequestBudget | None = None):
    url = f"{BUILDKITE_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(1, BK_GET_MAX_ATTEMPTS + 1):
        try:
            if budget:
                budget.charge("listing")
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ):
            if attempt == BK_GET_MAX_ATTEMPTS:
                raise
            wait = BK_GET_RETRY_BACKOFF_SECONDS * attempt
            log.warning(
                "Buildkite request failed on %s, retry %d/%d in %ds",
                path,
                attempt,
                BK_GET_MAX_ATTEMPTS,
                wait,
            )
            time.sleep(wait)
            continue

        if resp.status_code in BK_GET_RETRYABLE_STATUS_CODES:
            if attempt == BK_GET_MAX_ATTEMPTS:
                # Fail closed after retry exhaustion; in particular, never
                # translate a 429 into an apparently complete empty page.
                resp.raise_for_status()
            retry_after = _retry_wait(resp, attempt)
            log.warning(
                "Buildkite returned HTTP %d on %s, retry %d/%d in %ds",
                resp.status_code,
                path,
                attempt,
                BK_GET_MAX_ATTEMPTS,
                retry_after,
            )
            time.sleep(retry_after)
            continue

        resp.raise_for_status()
        _pace(resp)
        return resp.json()
    raise AssertionError("unreachable")


def _bk_paginate(
    path: str,
    token: str,
    params: dict | None = None,
    max_pages: int = 10,
    budget: RequestBudget | None = None,
):
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    params = dict(params or {})
    params.setdefault("per_page", 100)
    out: list = []
    for page in range(1, max_pages + 1):
        params["page"] = page
        items = _bk_get(path, token, params, budget=budget)
        if not isinstance(items, list):
            raise RuntimeError(f"Buildkite returned a non-list page for {path}")
        if not items:
            return out
        out.extend(items)
        if len(items) < params["per_page"]:
            return out
        if page == max_pages:
            raise RuntimeError(
                f"Buildkite pagination safety cap reached for {path} after {max_pages} full pages"
            )
    raise AssertionError("unreachable")


def _bk_result_artifacts(
    build_number: Any, token: str, budget: RequestBudget | None = None
) -> list[dict]:
    """A build's result artifacts: one filtered listing per path pattern,
    sharing one page cap."""
    path = (
        f"/organizations/{BUILDKITE_ORG}/pipelines/{BUILDKITE_PIPELINE_SLUG}"
        f"/builds/{build_number}/artifacts"
    )
    remaining_pages = _RESULT_ARTIFACT_MAX_PAGES
    artifacts: list[dict] = []
    for path_filter in _RESULT_ARTIFACT_PATHS:
        if remaining_pages < 1:
            raise RuntimeError(
                f"Buildkite result artifact discovery safety cap reached for {path} "
                f"after {_RESULT_ARTIFACT_MAX_PAGES} pages"
            )
        rows = _bk_paginate(
            path,
            token,
            {"path": path_filter, "per_page": 100},
            max_pages=remaining_pages,
            budget=budget,
        )
        # A successful listing ends on one short (possibly empty) page. Count
        # that page as well as every full page across all the filters.
        remaining_pages -= len(rows) // 100 + 1
        artifacts.extend(rows)
    return artifacts


# Each job's image, read once per run: a job uploads many artifacts.
_job_images: dict[str, str] = {}


def fetch_job_image(job_id: str, token: str, budget: RequestBudget | None = None) -> str:
    """The image a job ran in, or "" if its env cannot be read (the token
    lacks ``read_job_env``, or the job is gone)."""
    if not job_id:
        return ""
    if job_id not in _job_images:
        try:
            env = _bk_get(f"/organizations/{BUILDKITE_ORG}/jobs/{job_id}/env", token, budget=budget)
            image = job_image((env or {}).get("env") or {})
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in (403, 404):
                raise
            log.warning(
                "Cannot read job %s's env (HTTP %d); image unstated",
                job_id,
                exc.response.status_code,
            )
            image = ""
        _job_images[job_id] = image
    return _job_images[job_id]


def _bk_download_json(
    download_url: str,
    token: str,
    budget: RequestBudget | None = None,
    *,
    label: str = "",
) -> dict | None:
    """Download a JSON artifact; None if it is permanently broken (4xx, not JSON).

    Transient failures raise once retries run out: an ingested build is not
    listed again, so skipping would lose the artifact. Logs use ``label``,
    never the URL, which is signed after the redirect.
    """
    for attempt in range(1, BK_GET_MAX_ATTEMPTS + 1):
        if budget:
            budget.charge("download")
        try:
            resp = requests.get(
                download_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,
                allow_redirects=True,
            )
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ) as exc:
            if attempt == BK_GET_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"Download of artifact {label or '?'} failed after "
                    f"{BK_GET_MAX_ATTEMPTS} attempts: {type(exc).__name__}"
                ) from None
            time.sleep(BK_GET_RETRY_BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code in BK_GET_RETRYABLE_STATUS_CODES:
            if attempt == BK_GET_MAX_ATTEMPTS:
                raise RuntimeError(
                    f"Download of artifact {label or '?'} returned HTTP "
                    f"{resp.status_code} after {BK_GET_MAX_ATTEMPTS} attempts"
                )
            wait = _retry_wait(resp, attempt)
            log.warning(
                "Artifact %s returned HTTP %d, retry %d/%d in %ds",
                label or "?",
                resp.status_code,
                attempt,
                BK_GET_MAX_ATTEMPTS,
                wait,
            )
            time.sleep(wait)
            continue

        _pace(resp)
        if resp.status_code >= 400:
            log.warning("Skipping artifact %s: HTTP %d", label or "?", resp.status_code)
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("Skipping artifact %s: body is not JSON", label or "?")
            return None
    raise AssertionError("unreachable")


# (workload file, key) pairs already warned about in this run.
_warned_duplicate_keys: set[tuple[str, str]] = set()

# perf-eval's archive is well under a megabyte; anything this size is not it.
MAX_RECIPE_ARCHIVE_BYTES = 64 * 1024 * 1024


def fetch_workload_texts(gh_token: str, ref: str) -> dict[str, str]:
    """The text of every ``workloads/*.yaml`` at commit ``ref``, keyed by
    filename, from one archive of the repository: a single request per commit."""
    headers = {"Accept": "application/vnd.github+json"}
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"
    resp = requests.get(
        f"https://api.github.com/repos/{WORKLOAD_REPO}/tarball/{ref}",
        headers=headers,
        timeout=60,
        stream=True,
    )
    resp.raise_for_status()
    body = bytearray()
    for chunk in resp.iter_content(chunk_size=1 << 16):
        body.extend(chunk)
        if len(body) > MAX_RECIPE_ARCHIVE_BYTES:
            raise RuntimeError(
                f"{WORKLOAD_REPO} archive at {ref} exceeds {MAX_RECIPE_ARCHIVE_BYTES} bytes"
            )

    texts: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(bytes(body)), mode="r:gz") as archive:
        for member in archive.getmembers():
            # "<owner>-<repo>-<sha>/workloads/<file>": that directory only, not
            # its subdirectories. Read in memory, never extracted to disk.
            parts = PurePosixPath(member.name).parts
            if len(parts) != 3 or parts[1] != "workloads" or not member.isfile():
                continue
            if not parts[2].endswith((".yaml", ".yml")):
                continue
            handle = archive.extractfile(member)
            if handle is not None:
                texts[parts[2]] = handle.read().decode("utf-8")
    return texts


# Each model's checkpoint config.json, fetched once per run: many recipe
# commits share a model.
_checkpoint_configs: dict[str, dict | None] = {}


def fetch_checkpoint_config(model: str) -> dict | None:
    """A model's config.json from Hugging Face, or None if it cannot be read
    (gated without HF_TOKEN, missing, or unreachable). Read-only."""
    if model in _checkpoint_configs:
        return _checkpoint_configs[model]
    headers = {}
    if os.getenv("HF_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['HF_TOKEN']}"
    config = None
    try:
        resp = requests.get(
            f"https://huggingface.co/{model}/resolve/main/config.json", headers=headers, timeout=30
        )
        resp.raise_for_status()
        body = resp.json()
        config = body if isinstance(body, dict) else None
    except (requests.RequestException, ValueError, AttributeError) as exc:
        log.warning(
            "No checkpoint config for %s (%s); precision from the recipe only",
            model,
            type(exc).__name__,
        )
    _checkpoint_configs[model] = config
    return config


def fetch_workload_map(gh_token: str, ref: str) -> dict[str, tuple[dict, dict]]:
    """Every workload recipe at commit ``ref``, keyed by its ``name`` (which
    artifact paths use; the filename differs). A duplicated key is logged:
    YAML keeps only the last, as perf-eval does."""
    import yaml

    class RecipeLoader(yaml.SafeLoader):
        """SafeLoader that records duplicate mapping keys instead of hiding them."""

        def __init__(self, stream):
            super().__init__(stream)
            self.duplicate_keys: list[str] = []

        def construct_mapping(self, node, deep=False):
            seen: set = set()
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    self.duplicate_keys.append(str(key))
                seen.add(key)
            return super().construct_mapping(node, deep=deep)

    def load_recipe(text: str, name: str):
        loader = RecipeLoader(text)
        try:
            data = loader.get_single_data()
            for key in loader.duplicate_keys:
                # Once per run: a rebuild reads many recipe versions with the same fault.
                if (name, key) in _warned_duplicate_keys:
                    continue
                _warned_duplicate_keys.add((name, key))
                log.warning(
                    "Workload %s declares %r more than once (recipes at %s); YAML keeps "
                    "only the last, so the earlier block never runs and is missing from "
                    "coverage",
                    name,
                    key,
                    ref[:12],
                )
            return data
        finally:
            loader.dispose()

    out: dict[str, tuple[dict, dict]] = {}
    # Sorted, so a name declared by two files resolves the same way every run.
    for name, text in sorted(fetch_workload_texts(gh_token, ref).items()):
        try:
            data = load_recipe(text, name)
        except yaml.YAMLError as exc:
            log.warning("Skipping unparseable workload %s: %s", name, exc)
            continue
        if not isinstance(data, dict) or not data.get("name"):
            continue
        model = str(((data.get("vllm") or {}).get("model")) or "").strip()
        checkpoint = fetch_checkpoint_config(model) if model else None
        entry, configs = workload_entry(data, checkpoint)
        out[entry["name"]] = (entry, configs)
    log.info("Loaded %d workload recipes", len(out))
    return out


def collect(
    store_path: Path,
    *,
    days: int,
    bk_token: str,
    gh_token: str,
    dry_run: bool = False,
    recheck_builds: int = DEFAULT_RECHECK_BUILDS,
    budget: RequestBudget | None = None,
    rebuild: bool = False,
) -> int:
    """Pull AMD nightly artifacts and append the new events; return how many.

    ``dry_run`` lists and reports the cost but downloads and writes nothing.
    ``rebuild`` ignores the store, ingests the whole window again with the
    current code, and replaces the store with the result.
    """
    # No further back than the store keeps.
    if not 1 <= days <= WINDOW_DAYS:
        raise ValueError(f"perf-eval artifact lookback must be between 1 and {WINDOW_DAYS} days")
    # Anything older than the lookback would be lost.
    if rebuild and days != WINDOW_DAYS:
        raise ValueError(f"a rebuild must cover the whole {WINDOW_DAYS}-day window")
    budget = budget if budget is not None else RequestBudget()
    stored = read_events_strict(store_path)
    existing = [] if rebuild else stored
    seen = {event_key(e) for e in existing if e.get("event") in RESULT_EVENTS}
    known_artifacts = {key for event in existing for key in artifact_keys_from_event(event)}
    ingested_builds = {
        str(event["build_number"])
        for event in existing
        if event.get("event") in RESULT_EVENTS and event.get("build_number") is not None
    }
    before = len(existing)

    # Builds are read against the recipes they ran, not main's: parallelism,
    # which divides throughput per GPU, can change after a build.
    recipes_at: dict[str, dict[str, tuple[dict, dict]]] = {}

    def recipes_for(commit: str) -> dict[str, tuple[dict, dict]]:
        if commit not in recipes_at:
            recipes_at[commit] = fetch_workload_map(gh_token, ref=commit)
        return recipes_at[commit]

    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    builds = _bk_paginate(
        f"/organizations/{BUILDKITE_ORG}/pipelines/{BUILDKITE_PIPELINE_SLUG}/builds",
        bk_token,
        # By finish time, as the store keeps results: a nightly created before
        # the cutoff but finished after it is still in the store.
        {"branch": "main", "state": "finished", "finished_from": cutoff},
        budget=budget,
    )

    # Newest first, so the re-check window is the newest builds.
    nightlies = [(build, info) for build in builds if (info := nightly_info(build)) is not None]
    nightlies.sort(key=lambda pair: pair[0].get("number") or 0, reverse=True)
    log.info(
        "Examining %d finished builds since %s: %d nightlies",
        len(builds),
        cutoff,
        len(nightlies),
    )

    appended = 0
    markers_appended = 0
    discovered = 0
    would_download = 0
    pending_events: list[dict] = []

    # Every nightly, results or not, so the page can say when the newest one
    # failed or produced no AMD results. Recorded again only when it changes.
    recorded_runs = {
        str(e.get("build_number")): e.get("state")
        for e in existing
        if e.get("event") == NIGHTLY_RUN_EVENT
    }
    for build, night in nightlies:
        if recorded_runs.get(str(build.get("number"))) != (build.get("state") or ""):
            pending_events.append(nightly_run(build, night))

    for position, (build, night) in enumerate(nightlies):
        number = build.get("number")
        if position >= recheck_builds and str(number) in ingested_builds:
            budget.skipped_builds += 1
            continue
        if not build.get("commit"):
            log.warning("Build #%s has no perf-eval commit, so no recipes; skipping", number)
            continue
        identity = {
            "build_number": number,
            "build_url": build.get("web_url") or "",
            "build_commit": build["commit"],  # perf-eval repo commit
            "branch": night["branch"],
            "vllm_commit": night["vllm_commit"],
            "date": build.get("finished_at") or "",
            "nightly_date": night["nightly_date"],
        }
        for artifact in _bk_result_artifacts(number, bk_token, budget=budget):
            kind = classify_artifact(artifact.get("path") or "")
            if kind is None:
                continue
            discovered += 1
            provenance = artifact_provenance(artifact, number)
            source_key = artifact_key(provenance)
            if source_key is not None and source_key in known_artifacts:
                continue
            _, workload, tail = kind
            if not is_amd_workload(workload=workload):
                continue
            recipe = recipes_for(identity["build_commit"]).get(workload)
            if recipe is None:
                log.warning("No recipe for workload %s (build #%s); skipping", workload, number)
                continue
            entry, configs = recipe
            if kind[0] == "perf" and tail not in configs:
                log.warning(
                    "No run %s in workload %s (build #%s); skipping", tail, workload, number
                )
                continue
            would_download += 1
            if dry_run:
                continue
            payload = _bk_download_json(
                artifact.get("download_url") or "",
                bk_token,
                budget=budget,
                label=f"#{number} {provenance['buildkite_artifact_path']}",
            )
            if payload is None:
                continue
            job = provenance["buildkite_artifact_job_id"]
            ran = {**identity, "image": fetch_job_image(job, bk_token, budget=budget)}
            if kind[0] == "perf":
                event = perf_event(payload, entry=entry, config=configs[tail], identity=ran)
            else:
                event = accuracy_event(
                    payload, workload=workload, task=tail, entry=entry, identity=ran
                )
            if event is None:
                continue
            key = event_key(event)
            if key not in seen:
                event.update({field: provenance[field] for field in _ARTIFACT_PROVENANCE_FIELDS})
                pending_events.append(event)
                seen.add(key)
                appended += 1
            elif source_key is not None:
                # Already stored without its artifact ID; record just the ID.
                pending_events.append(artifact_marker(provenance))
                markers_appended += 1
            if source_key is not None:
                known_artifacts.add(source_key)

    if dry_run:
        log.info(
            "DRY RUN — nothing downloaded, nothing written.\n"
            "  nightlies in window ...... %d\n"
            "  re-listed ................ %d (newest %d always, plus any not yet ingested)\n"
            "  skipped as ingested ...... %d\n"
            "  result artifacts seen .... %d\n"
            "  would download ........... %d\n"
            "  cost so far .............. %s\n"
            "  a real run would cost .... %d requests total",
            len(nightlies),
            len(nightlies) - budget.skipped_builds,
            recheck_builds,
            budget.skipped_builds,
            discovered,
            would_download,
            budget.summary(),
            budget.total + would_download,
        )
        return 0

    # Coverage expects what the latest nightly's recipes define. Recipes at a
    # commit never change, so a snapshot is only taken for a new commit.
    epoch = datetime.min.replace(tzinfo=UTC)
    latest = max(
        (e for e in (*existing, *pending_events) if e.get("event") == "perf_result"),
        key=lambda e: finished_at(e) or epoch,
        default=None,
    )
    previous_expected = max(
        (e for e in existing if e.get("event") == EXPECTED_CONFIGS_EVENT),
        key=lambda e: received_at(e) or epoch,
        default=None,
    )
    recipe_commit = (latest or {}).get("build_commit") or ""
    new_commit = (previous_expected or {}).get("recipe_commit") != recipe_commit
    # Snapshots from before accuracy coverage lack the key; refetch those once.
    lacks_accuracy = previous_expected is not None and "accuracy" not in previous_expected
    if recipe_commit and (new_commit or lacks_accuracy):
        recipes = recipes_for(recipe_commit)
        expected = expected_configs(recipes)
        if expected:
            pending_events.append(
                {
                    "event": EXPECTED_CONFIGS_EVENT,
                    "received_at": utcnow_iso(),
                    "recipe_commit": recipe_commit,
                    "configs": expected,
                    "accuracy": expected_accuracy(recipes),
                }
            )

    if rebuild:
        _replace_store(store_path, stored, pending_events)
        return appended

    # Rewrite even with nothing new, so results that aged out are dropped.
    append_events(store_path, pending_events)
    log.info(
        "Appended %d new result events and %d artifact markers (%d existing records). "
        "Skipped re-listing %d already-ingested builds. Cost: %s",
        appended,
        markers_appended,
        before,
        budget.skipped_builds,
        budget.summary(),
    )
    return appended


def _replace_store(store_path: Path, stored: list[dict], rebuilt: list[dict]) -> None:
    """Replace the store with a rebuild, unless the rebuild found fewer results."""

    def result_count(events: list[dict]) -> int:
        return sum(1 for e in compact_events(events) if e.get("event") in RESULT_EVENTS)

    before, after = result_count(stored), result_count(rebuilt)
    # Fewer means Buildkite no longer has some artifacts, or the new code drops
    # results; either way the rebuild would lose data, so keep the store.
    if after < before:
        raise RuntimeError(
            f"Rebuild found {after} results but the store holds {before}; "
            "keeping the store. Find out what the rebuild is missing before replacing it."
        )
    write_events_atomic(store_path, rebuilt)
    log.info("Rebuilt the store: %d results (was %d).", after, before)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=WINDOW_DAYS,
        help=f"Lookback window in days, 1-{WINDOW_DAYS} (default: {WINDOW_DAYS})",
    )
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="Path to events.jsonl")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List and report the request cost without downloading or writing anything",
    )
    parser.add_argument(
        "--recheck-builds",
        type=int,
        default=DEFAULT_RECHECK_BUILDS,
        help=(
            "Always re-list the newest N nightlies even if already ingested, to catch "
            f"artifacts from a retried job (default: {DEFAULT_RECHECK_BUILDS})"
        ),
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=DEFAULT_MAX_REQUESTS,
        help=(
            "Abort if outbound Buildkite requests would exceed this "
            f"(default: {DEFAULT_MAX_REQUESTS}; 0 disables the ceiling)"
        ),
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            f"Ignore the store, ingest the whole {WINDOW_DAYS}-day window again, and replace "
            "the store, unless that finds fewer results than it holds"
        ),
    )
    args = parser.parse_args()
    if not 0 <= args.recheck_builds <= MAX_RECHECK_BUILDS:
        parser.error(f"--recheck-builds must be from 0 to {MAX_RECHECK_BUILDS}")

    # Before any connection is made.
    use_system_certificates()

    bk_token = os.getenv("BUILDKITE_TOKEN") or ""
    if not bk_token:
        log.error("BUILDKITE_TOKEN not set; cannot pull perf-eval artifacts")
        return 1
    gh_token = os.getenv("GITHUB_TOKEN") or ""

    collect(
        Path(args.store),
        days=args.days,
        bk_token=bk_token,
        gh_token=gh_token,
        dry_run=args.dry_run,
        recheck_builds=args.recheck_builds,
        budget=RequestBudget(args.max_requests or None),
        rebuild=args.rebuild,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
