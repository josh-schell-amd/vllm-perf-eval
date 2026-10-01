"""Unit tests for the Databricks row -> canonical event adapter.

Row shapes are taken from real sampled rows of both tables (captured while
building this collector); see docs/data-pipeline.md.
"""

from __future__ import annotations

from perf_eval.databricks_collect import (
    accuracy_event,
    commit_from_image,
    day_bucket,
    is_nightly_row,
    perf_event,
    perf_parallelism,
)

# A real vllm_perf_data_ingest row (lib/ingest_perf.py's payload shape).
PERF_ROW = {
    "conc": 128,
    "date": "2026-09-30 17:44:42",
    "device": "mi300x",
    "disagg": "false",
    "dp_attention": "false",
    "ep": 1,
    "framework": "vllm",
    "image": "vllm/vllm-openai-rocm:nightly-ac68c3087215e0a4f3cdfa218508c6aada57235d",
    "input_tput_per_gpu": 130.21526637990956,
    "is_multinode": "false",
    "isl": 1024,
    "mean_intvty": 8.323526688425265,
    "mean_itl": 0.1201413820647208,
    "mean_tpot": 0.1201413820647208,
    "mean_ttft": 2.840227181401133,
    "median_intvty": 8.462457708604006,
    "median_itl": 0.10379998851567508,
    "median_tpot": 0.11816898050589646,
    "median_ttft": 1.76025066152215,
    "model": "moonshotai/Kimi-K2.5",
    "nightly": True,
    "osl": 1024,
    "output_tput_per_gpu": 130.21526637990956,
    "p99_intvty": 7.796225042229961,
    "p99_itl": 0.18108138488605616,
    "p99_tpot": 0.1282672055492602,
    "p99_ttft": 10.01643297959119,
    "precision": "bf16",
    "spec_decoding": "false",
    "std_intvty": 295.9446411856773,
    "std_itl": 0.0671920366007524,
    "std_tpot": 0.003379010331099709,
    "std_ttft": 2.4172351967096253,
    "tp": 8,
    "tput_per_gpu": 260.43053275981913,
}

# A real vllm_eval_data_ingest row's metadata (kind="results" synthesized from
# the confirmed metadata shape of a live "samples" row; lib/ingest.py uses the
# same metadata() for both kinds).
EVAL_ROW = {
    "buildkite_branch": "main",
    "buildkite_build_id": "01a0f0e6-0961-4ed9-a9f1-1f172bec1fec",
    "buildkite_build_number": "617",
    "buildkite_build_url": "https://buildkite.com/vllm/perf-eval/builds/617",
    "buildkite_commit": "086bacd132e4f04fa112df5610bfae2db01ba5cd",
    "buildkite_pipeline_slug": "perf-eval",
    "image": "vllm/vllm-openai-rocm:nightly-ac68c3087215e0a4f3cdfa218508c6aada57235d",
    "kind": "results",
    "nightly": True,
    "task": "gsm8k",
    "vllm_commit": "ac68c3087215e0a4f3cdfa218508c6aada57235d",
    "workload": "kimi_k2_5-mi300x",
    "_ingest_timestamp": "2026-09-30T17:44:42.255335868Z",
    "data": {
        "results": {
            "gsm8k": {
                "exact_match,strict-match": 0.82,
                "exact_match_stderr,strict-match": 0.01,
                "alias": "gsm8k",
            }
        }
    },
}

RECIPES = {
    "kimi_k2_5-mi300x": (
        {"model": "moonshotai/Kimi-K2.5", "device": "mi300x"},
        {},
    )
}


class TestCommitFromImage:
    def test_release_tag_convention(self):
        image = (
            "public.ecr.aws/q9t5s3a7/vllm-release-repo:"
            "ac68c3087215e0a4f3cdfa218508c6aada57235d-x86_64"
        )
        assert commit_from_image(image) == "ac68c3087215e0a4f3cdfa218508c6aada57235d"

    def test_nightly_tag_convention(self):
        image = "vllm/vllm-openai-rocm:nightly-ac68c3087215e0a4f3cdfa218508c6aada57235d"
        assert commit_from_image(image) == "ac68c3087215e0a4f3cdfa218508c6aada57235d"

    def test_no_commit_in_tag(self):
        assert commit_from_image("vllm/vllm-openai:latest") == ""

    def test_empty_image(self):
        assert commit_from_image("") == ""


class TestDayBucket:
    def test_space_separated_timestamp(self):
        assert day_bucket("2026-09-30 17:44:42") == "2026-09-30"

    def test_iso_timestamp_with_fractional_seconds(self):
        assert day_bucket("2026-09-30T17:44:42.255335868Z") == "2026-09-30"

    def test_empty(self):
        assert day_bucket("") == ""


class TestIsNightlyRow:
    def test_true(self):
        assert is_nightly_row({"nightly": True})

    def test_false_missing_or_non_boolean(self):
        assert not is_nightly_row({})
        assert not is_nightly_row({"nightly": False})
        assert not is_nightly_row({"nightly": "true"})
        assert not is_nightly_row({"nightly": 1})


class TestPerfParallelism:
    def test_default_is_empty(self):
        assert perf_parallelism({"tp": 1, "ep": 1, "dp_attention": "false"}) == {}

    def test_tensor_parallel(self):
        assert perf_parallelism({"tp": 8, "ep": 1, "dp_attention": "false"}) == {
            "tensor_parallel_size": 8
        }

    def test_expert_parallel_flag(self):
        parallelism = perf_parallelism({"tp": 8, "ep": 4, "dp_attention": "false"})
        assert parallelism == {"tensor_parallel_size": 8, "enable_expert_parallel": True}

    def test_dp_attention_flag(self):
        parallelism = perf_parallelism({"tp": 1, "ep": 1, "dp_attention": "true"})
        assert parallelism == {"dp_attention": True}


class TestPerfEvent:
    def test_builds_a_canonical_event(self):
        event = perf_event(dict(PERF_ROW))
        assert event is not None
        assert event["event"] == "perf_result"
        assert event["model"] == "moonshotai/Kimi-K2.5"
        assert event["device"] == "mi300x"
        assert event["precision"] == "bf16"
        assert event["parallelism"] == {"tensor_parallel_size": 8}
        assert event["isl"] == 1024 and event["osl"] == 1024 and event["conc"] == 128
        assert event["date"] == "2026-09-30 17:44:42"
        assert event["nightly_date"] == event["build_number"] == "2026-09-30"
        assert event["vllm_commit"] == "ac68c3087215e0a4f3cdfa218508c6aada57235d"
        assert event["metrics"]["tput_per_gpu"] == PERF_ROW["tput_per_gpu"]
        assert event["metrics"]["mean_ttft"] == PERF_ROW["mean_ttft"]

    def test_non_nightly_row_is_dropped(self):
        row = {**PERF_ROW, "nightly": False}
        assert perf_event(row) is None

    def test_non_amd_row_is_dropped(self):
        row = {
            **PERF_ROW,
            "device": "h200",
            "image": "public.ecr.aws/q9t5s3a7/vllm-release-repo:ac68c30-x86_64",
        }
        assert perf_event(row) is None

    def test_no_metrics_is_dropped(self):
        row = {
            k: v
            for k, v in PERF_ROW.items()
            if k not in ("tput_per_gpu",)
            and "tpot" not in k
            and "ttft" not in k
            and "itl" not in k
            and "intvty" not in k
            and "tput" not in k
        }
        assert perf_event(row) is None


class TestAccuracyEvent:
    def test_builds_a_canonical_event_via_recipe_join(self):
        event = accuracy_event(dict(EVAL_ROW), recipes=RECIPES)
        assert event is not None
        assert event["event"] == "accuracy_result"
        assert event["model"] == "moonshotai/Kimi-K2.5"
        assert event["device"] == "mi300x"
        assert event["workload"] == "kimi_k2_5-mi300x"
        assert event["task"] == "gsm8k"
        assert event["build_url"] == "https://buildkite.com/vllm/perf-eval/builds/617"
        assert event["branch"] == "main"
        assert event["vllm_commit"] == "ac68c3087215e0a4f3cdfa218508c6aada57235d"
        assert event["nightly_date"] == event["build_number"] == "2026-09-30"
        assert event["results"] == [
            {"task": "gsm8k", "metric": "exact_match,strict-match", "value": 0.82, "primary": True}
        ]

    def test_samples_kind_is_dropped(self):
        row = {**EVAL_ROW, "kind": "samples"}
        assert accuracy_event(row, recipes=RECIPES) is None

    def test_non_nightly_row_is_dropped(self):
        row = {**EVAL_ROW, "nightly": False}
        assert accuracy_event(row, recipes=RECIPES) is None

    def test_unknown_workload_with_no_amd_marker_is_dropped(self):
        row = {
            **EVAL_ROW,
            "workload": "some_other_workload",
            "device": "",
            "image": "vllm/vllm-openai:nightly-ac68c3087215e0a4f3cdfa218508c6aada57235d",
        }
        assert accuracy_event(row, recipes={}) is None

    def test_unknown_workload_whose_name_still_marks_it_amd_is_kept_unlabeled(self):
        # The workload stem still carries the "_mi300x" marker even without a
        # recipe match, so scope is satisfied; model/device are just blank.
        row = {**EVAL_ROW, "workload": "renamed_workload_mi300x"}
        event = accuracy_event(row, recipes={})
        assert event is not None
        assert event["model"] == ""
        assert event["device"] == ""
