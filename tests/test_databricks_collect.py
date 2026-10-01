"""Unit tests for the Databricks row -> canonical event adapter.

Row shapes are taken from real sampled rows of both tables (captured while
building this collector); see docs/data-pipeline.md.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from perf_eval import databricks_collect
from perf_eval.databricks_collect import (
    EVAL_TABLE,
    accuracy_event,
    commit_from_image,
    day_bucket,
    fetch_rows,
    is_nightly_row,
    perf_event,
    perf_parallelism,
    recipe_labels,
    run_query,
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
        {
            "model": "moonshotai/Kimi-K2.5",
            "device": "mi300x",
            "precision": "int4",
            "parallelism": {"tensor_parallel_size": 8},
            "bench_tp": 8,
        },
        {"bench-conc-128": {"isl": 1024, "osl": 1024, "conc": 128}},
    )
}
LABELS = recipe_labels(RECIPES)


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
        event = perf_event(dict(PERF_ROW), labels=LABELS)
        assert event is not None
        assert event["event"] == "perf_result"
        assert event["model"] == "moonshotai/Kimi-K2.5"
        assert event["device"] == "mi300x"
        # The recipe's precision, not the row's bf16 fallback.
        assert event["precision"] == "int4"
        assert event["parallelism"] == {"tensor_parallel_size": 8}
        assert event["isl"] == 1024 and event["osl"] == 1024 and event["conc"] == 128
        assert event["date"] == "2026-09-30 17:44:42"
        assert event["nightly_date"] == event["build_number"] == "2026-09-30"
        assert event["vllm_commit"] == "ac68c3087215e0a4f3cdfa218508c6aada57235d"
        assert event["metrics"]["tput_per_gpu"] == PERF_ROW["tput_per_gpu"]
        assert event["metrics"]["mean_ttft"] == PERF_ROW["mean_ttft"]

    def test_non_nightly_row_is_dropped(self):
        row = {**PERF_ROW, "nightly": False}
        assert perf_event(row, labels=LABELS) is None

    def test_non_amd_row_is_dropped(self):
        row = {
            **PERF_ROW,
            "device": "h200",
            "image": "public.ecr.aws/q9t5s3a7/vllm-release-repo:ac68c30-x86_64",
        }
        assert perf_event(row, labels=LABELS) is None

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
        assert perf_event(row, labels=LABELS) is None

    def test_no_matching_recipe_keeps_the_row_with_precision_unstated(self):
        drops = Counter()
        event = perf_event({**PERF_ROW, "conc": 7}, labels=LABELS, drops=drops)
        assert event is not None
        assert event["precision"] == ""
        assert event["parallelism"] == {"tensor_parallel_size": 8}
        assert drops == {"perf: kept, no matching recipe (precision unstated)": 1}

    def test_dropped_rows_are_counted_by_reason(self):
        drops = Counter()
        perf_event({**PERF_ROW, "nightly": False}, labels=LABELS, drops=drops)
        assert drops == {"perf: not nightly": 1}


class TestRecipeLabels:
    def test_tp_is_perf_evals_tp_times_dp(self):
        entry = {**RECIPES["kimi_k2_5-mi300x"][0], "bench_tp": 8}
        entry["parallelism"] = {"tensor_parallel_size": 4, "data_parallel_size": 2}
        labels = recipe_labels({"w": (entry, RECIPES["kimi_k2_5-mi300x"][1])})
        event = perf_event(dict(PERF_ROW), labels=labels)
        assert event is not None
        assert event["parallelism"] == {"tensor_parallel_size": 4, "data_parallel_size": 2}

    def test_a_shape_two_recipes_label_differently_is_left_out(self):
        entry, configs = RECIPES["kimi_k2_5-mi300x"]
        labels = recipe_labels(
            {"a": (entry, configs), "b": ({**entry, "precision": "fp8"}, configs)}
        )
        assert labels == {}


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

    def test_dropped_rows_are_counted_by_reason(self):
        drops = Counter()
        accuracy_event({**EVAL_ROW, "data": {"results": {}}}, recipes=RECIPES, drops=drops)
        assert drops == {"eval: no scores": 1}

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


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeSession:
    """Serves canned Statement Execution API responses, in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append(("POST", url, json))
        return FakeResponse(self.responses.pop(0))

    def get(self, url, timeout):
        self.calls.append(("GET", url, None))
        return FakeResponse(self.responses.pop(0))


@pytest.fixture
def databricks_env(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "example.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh")
    monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    monkeypatch.setattr(databricks_collect.time, "sleep", lambda _: None)

    def serve(*responses):
        session = FakeSession(responses)
        monkeypatch.setattr(databricks_collect.requests, "Session", lambda: session)
        return session

    return serve


def _succeeded(rows, total=None, next_link=None):
    result = {"data_array": rows}
    if next_link:
        result["next_chunk_internal_link"] = next_link
    return {
        "statement_id": "s1",
        "status": {"state": "SUCCEEDED"},
        "manifest": {"total_row_count": len(rows) if total is None else total},
        "result": result,
    }


class TestRunQuery:
    def test_polls_until_done_then_follows_every_chunk(self, databricks_env):
        session = databricks_env(
            {"statement_id": "s1", "status": {"state": "RUNNING"}},
            _succeeded([["a"]], total=2, next_link="/api/2.0/sql/statements/s1/result/chunks/1"),
            {"data_array": [["b"]]},
        )
        assert run_query("SELECT 1", {}) == [["a"], ["b"]]
        assert session.calls[0][1] == "https://example.cloud.databricks.com/api/2.0/sql/statements"
        assert session.calls[1][1].endswith("/statements/s1")
        assert session.calls[2][1].endswith("/chunks/1")

    def test_fails_when_rows_go_missing(self, databricks_env):
        databricks_env(_succeeded([["a"]], total=2))
        with pytest.raises(RuntimeError, match="reported 2 rows"):
            run_query("SELECT 1", {})

    def test_fails_on_a_truncated_result(self, databricks_env):
        body = _succeeded([["a"]])
        body["manifest"]["truncated"] = True
        databricks_env(body)
        with pytest.raises(RuntimeError, match="truncated"):
            run_query("SELECT 1", {})

    def test_fails_on_a_failed_statement(self, databricks_env):
        databricks_env(
            {"statement_id": "s1", "status": {"state": "FAILED", "error": {"message": "boom"}}}
        )
        with pytest.raises(RuntimeError, match="FAILED: boom"):
            run_query("SELECT 1", {})


class TestFetchRows:
    def test_rebuilds_the_selected_fields_with_their_json_types(self, databricks_env):
        results = {"gsm8k": {"exact_match,strict-match": 0.82}}
        session = databricks_env(
            _succeeded([["2026-09-30T17:44:42Z", '"results"', "true", json.dumps(results)]])
        )
        since = databricks_collect.datetime(2026, 9, 1, tzinfo=databricks_collect.UTC)
        rows = fetch_rows(EVAL_TABLE, since=since, fields=("kind", "nightly", "data.results"))
        assert rows == [
            {
                "kind": "results",
                "nightly": True,
                "data": {"results": results},
                "_ingest_timestamp": "2026-09-30T17:44:42Z",
            }
        ]
        statement = session.calls[0][2]["statement"]
        assert "message:kind::string = 'results'" in statement
        assert statement.lstrip().upper().startswith("SELECT")
