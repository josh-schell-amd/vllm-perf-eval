"""The AMD nightly perf-eval dashboard's pipeline.

Scope: AMD (MI-series) workloads from scheduled nightly builds only; NVIDIA
workloads and ad-hoc builds from the same pipeline are excluded.
"""

# Cosmetic only: results come from Databricks (see databricks_collect.py),
# not from the Buildkite API, but this is still the CI pipeline that produced
# them, and the payload names it so the page can link to it.
BUILDKITE_ORG = "vllm"
BUILDKITE_PIPELINE_SLUG = "perf-eval"
PIPELINE_URL = f"https://buildkite.com/{BUILDKITE_ORG}/{BUILDKITE_PIPELINE_SLUG}"

# Public repo holding the workload recipes.
WORKLOAD_REPO = "vllm-project/perf-eval"

# Days of nightlies shown and collected. Databricks retains full history, so
# this is only a display/query window now, not a retention limit.
WINDOW_DAYS = 30
