"""Workload recipes from the public `vllm-project/perf-eval` repo.

Results come from Databricks (see `databricks_collect.py`), but recipes are
the only source of what results can't carry correctly themselves: what should
run (the coverage card), a perf row's precision and parallelism, and an
accuracy row's model/device (see docs/data-pipeline.md).

Recipes are read at each result's build's perf-eval commit (the
`buildkite_commit` its eval rows carry), so a result is labeled by the
recipe that ran it, and Coverage uses the newest build's commit. `main` is
read only for a result whose build is unknown.
"""

from __future__ import annotations

import io
import logging
import os
import tarfile
from pathlib import PurePosixPath

import requests

from perf_eval import WORKLOAD_REPO
from perf_eval.normalize import is_amd_workload, parallelism_of, to_int

log = logging.getLogger(__name__)


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

    Runs are named ``<config>-conc-<concurrency>``, as perf-eval expands them.
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
    # The `tp` perf-eval stamps on a Databricks perf row (BENCH_TP in its
    # lib/parse_workload.py): metadata.tp if set, else TP x DP. Perf rows are
    # matched back to their recipe on it.
    bench_tp = to_int(meta.get("tp")) or (parallelism.get("tensor_parallel_size") or 1) * (
        parallelism.get("data_parallel_size") or 1
    )
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
        "bench_tp": bench_tp,
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


# perf-eval's archive is well under a megabyte; anything this size is not it.
MAX_RECIPE_ARCHIVE_BYTES = 64 * 1024 * 1024

# (workload file, key) pairs already warned about in this run.
_warned_duplicate_keys: set[tuple[str, str]] = set()

# Each model's checkpoint config.json, fetched once per run: many recipes share a model.
_checkpoint_configs: dict[str, dict | None] = {}


def fetch_workload_texts(gh_token: str, ref: str) -> dict[str, str]:
    """The text of every ``workloads/*.yaml`` at commit/ref ``ref``, keyed by
    filename, from one archive of the repository: a single request per ref."""
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
    """Every workload recipe at commit/ref ``ref``, keyed by its ``name`` (which
    results use, via `workload`; the filename differs). A duplicated key is
    logged: YAML keeps only the last, as perf-eval does."""
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
