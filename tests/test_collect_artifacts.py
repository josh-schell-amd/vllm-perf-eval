"""Tests for the Buildkite artifact collector's pure logic.

No network is touched: every helper exercised here is I/O-free by design.
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from perf_eval import collect_artifacts as ca

COMMIT = "93d8f834dd8acf33eb0e2a75b2711b628cb6e226"
NIGHTLY_MESSAGE = f"Nightly run 2026-06-30: commit {COMMIT}"


def build(**overrides):
    payload = {
        "number": 42,
        "branch": "main",
        "message": NIGHTLY_MESSAGE,
        "state": "finished",
        "source": "schedule",
        "web_url": "https://buildkite.com/vllm/perf-eval/builds/42",
        "commit": "f" * 40,
        "env": {"NIGHTLY": "1"},
    }
    payload.update(overrides)
    return payload


class TestNightlyScopeFilter:
    def test_nightly_env_on_main_is_a_nightly(self):
        assert ca.is_nightly_build(build()) is True

    def test_nightly_env_with_an_unstructured_message_is_a_nightly(self):
        assert ca.is_nightly_build(build(message="manual")) is True

    def test_message_alone_is_not_a_nightly(self):
        assert ca.is_nightly_build(build(env={})) is False

    def test_scheduled_mention_of_nightly_is_not_a_nightly(self):
        assert (
            ca.is_nightly_build(build(message="nightly sweep", source="schedule", env={})) is False
        )

    def test_adhoc_build_is_not_a_nightly(self):
        assert ca.is_nightly_build(build(message="debug run", source="ui", env={})) is False

    def test_nightly_env_off_main_is_not_a_nightly(self):
        assert ca.is_nightly_build(build(branch="feature/x")) is False

    def test_nightly_message_off_main_is_not_a_nightly(self):
        assert ca.is_nightly_build(build(branch="release")) is False

    def test_non_nightly_returns_no_info(self):
        assert ca.nightly_info(build(env={})) is None


class TestNightlyInfo:
    def test_extracts_the_commit_from_the_message(self):
        info = ca.nightly_info(build())
        assert info == {"vllm_commit": COMMIT, "branch": "main", "nightly_date": "2026-06-30"}

    def test_a_nightly_whose_message_names_no_date_has_none(self):
        info = ca.nightly_info(build(message="manual", env={"NIGHTLY": "1", "VLLM_COMMIT": COMMIT}))
        assert info is not None
        assert info["nightly_date"] == ""

    def test_falls_back_to_vllm_commit_env(self):
        info = ca.nightly_info(build(message="manual", env={"NIGHTLY": "1", "VLLM_COMMIT": COMMIT}))
        assert info is not None
        assert info["vllm_commit"] == COMMIT

    def test_the_image_tag_is_not_parsed_for_a_commit(self):
        # perf-eval always passes VLLM_COMMIT to a nightly; without it the
        # nightly is identified by build number instead.
        info = ca.nightly_info(
            build(
                message="manual",
                env={"NIGHTLY": "1", "VLLM_IMAGE": f"vllm/vllm-openai-rocm:nightly-{COMMIT}"},
            )
        )
        assert info is not None
        assert info["vllm_commit"] == ""


def plugins_env(*images: str) -> dict:
    """A job env shaped like perf-eval's AMD Kubernetes step."""
    plugin = {
        "github.com/buildkite-plugins/kubernetes-buildkite-plugin": {
            "podSpecPatch": {
                "containers": [{"name": f"c{i}", "image": image} for i, image in enumerate(images)]
            }
        }
    }
    return {
        "VLLM_IMAGE": f"public.ecr.aws/q9t5s3a7/vllm-release-repo:{COMMIT}-x86_64",
        "BUILDKITE_PLUGINS": json.dumps([plugin]),
    }


class TestJobImage:
    def test_is_the_container_the_job_ran_not_the_builds_cuda_image(self):
        image = f"vllm/vllm-openai-rocm:nightly-{COMMIT}"
        assert ca.job_image(plugins_env(image)) == image

    def test_a_pinned_image_is_reported_as_is(self):
        assert (
            ca.job_image(plugins_env("vllm/vllm-openai-rocm:kimi-k3"))
            == "vllm/vllm-openai-rocm:kimi-k3"
        )

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"BUILDKITE_PLUGINS": "not json"},
            {"VLLM_IMAGE": "vllm/vllm-openai-rocm:nightly"},
            plugins_env("a:1", "b:2"),
        ],
    )
    def test_unstated_unless_the_job_names_exactly_one(self, env):
        assert ca.job_image(env) == ""


class TestFetchJobImage:
    @pytest.fixture(autouse=True)
    def fresh_cache(self, monkeypatch):
        monkeypatch.setattr(ca, "_job_images", {})

    def _http_error(self, status):
        response = ca.requests.Response()
        response.status_code = status
        return ca.requests.HTTPError(response=response)

    @pytest.mark.parametrize("status", [403, 404])
    def test_an_unreadable_env_leaves_the_image_unstated(self, monkeypatch, status):
        def denied(*_args, **_kwargs):
            raise self._http_error(status)

        monkeypatch.setattr(ca, "_bk_get", denied)
        assert ca.fetch_job_image("job-1", "t") == ""

    def test_other_failures_fail_the_run(self, monkeypatch):
        # An ingested artifact is not downloaded again, so a lost image stays lost.
        def broken(*_args, **_kwargs):
            raise self._http_error(500)

        monkeypatch.setattr(ca, "_bk_get", broken)
        with pytest.raises(ca.requests.HTTPError):
            ca.fetch_job_image("job-1", "t")

    def test_reads_each_job_once(self, monkeypatch):
        calls = []

        def get(path, _token, budget=None):
            calls.append(path)
            return {"env": plugins_env("vllm/vllm-openai-rocm:x")}

        monkeypatch.setattr(ca, "_bk_get", get)
        assert ca.fetch_job_image("job-1", "t") == "vllm/vllm-openai-rocm:x"
        assert ca.fetch_job_image("job-1", "t") == "vllm/vllm-openai-rocm:x"
        assert calls == ["/organizations/vllm/jobs/job-1/env"]


class TestClassifyArtifact:
    @pytest.mark.parametrize("prefix", ["", "./"])
    def test_perf_bench_artifact(self, prefix):
        result = ca.classify_artifact(
            f"{prefix}results/minimax_m2_5-mi355x/bench-8k-in-1k-out.json"
        )
        assert result == ("perf", "minimax_m2_5-mi355x", "8k-in-1k-out")

    def test_accuracy_artifact(self):
        result = ca.classify_artifact("results/minimax_m2_5-mi355x/gsm8k/results_2026-01-01.json")
        assert result == ("accuracy", "minimax_m2_5-mi355x", "gsm8k")

    @pytest.mark.parametrize("prefix", ["", "./"])
    def test_accuracy_artifact_in_lm_evals_model_directory(self, prefix):
        # The layout a real nightly produces: lm-eval nests its output under a
        # directory named after the sanitized model id.
        result = ca.classify_artifact(
            f"{prefix}results/gpt_oss_120b-mi355x/gsm8k/openai__gpt-oss-120b/"
            "results_2026-09-22T13-04-18.930123.json"
        )
        assert result == ("accuracy", "gpt_oss_120b-mi355x", "gsm8k")

    @pytest.mark.parametrize(
        "path",
        [
            "results/wl/gsm8k/samples_gsm8k.jsonl",  # large, never downloaded
            "results/wl/gsm8k/model/samples_gsm8k_2026-01-01.jsonl",
            "results/wl/gsm8k/a/b/results_2026-01-01.json",  # deeper than lm-eval writes
            "results/wl/aiperf-profile/profile_export.json",
            "logs/build.txt",
            "",
        ],
    )
    def test_unrecognized_paths_are_skipped(self, path):
        assert ca.classify_artifact(path) is None


class TestParseParallelism:
    @pytest.mark.parametrize(
        "serve_args,expected",
        [
            ("--tensor-parallel-size 8", {"tensor_parallel_size": 8}),
            ("--tensor-parallel-size=8", {"tensor_parallel_size": 8}),
            ("-tp 4", {"tensor_parallel_size": 4}),
            ("--tp 2", {"tensor_parallel_size": 2}),
            (
                "--tensor-parallel-size 4 --data-parallel-size 2",
                {"tensor_parallel_size": 4, "data_parallel_size": 2},
            ),
            (
                "--data-parallel-size 8 --enable-expert-parallel --trust-remote-code",
                {"data_parallel_size": 8, "enable_expert_parallel": True},
            ),
            (
                "-pp 2 -dcp 2 -pcp 2",
                {
                    "pipeline_parallel_size": 2,
                    "decode_context_parallel_size": 2,
                    "prefill_context_parallel_size": 2,
                },
            ),
            # Defaults are left out, so writing one out does not start a new series.
            ("--tensor-parallel-size 1 --no-enable-expert-parallel", {}),
            ("", {}),
            ("--tensor-parallel-size abc", {}),
            ("--tensor-parallel-size", {}),
        ],
    )
    def test_every_parallelism_flag_is_recorded(self, serve_args, expected):
        assert ca.parse_parallelism(serve_args) == expected

    def test_a_parallelism_flag_this_code_has_never_seen_is_recorded(self):
        parsed = ca.parse_parallelism("--tensor-parallel-size 8 --future-parallel-mode ring")
        assert parsed == {"tensor_parallel_size": 8, "future_parallel_mode": "ring"}

    def test_data_parallel_launch_settings_are_not_configuration(self):
        serve_args = (
            "--data-parallel-size 2 --data-parallel-address 10.0.0.1 --data-parallel-rpc-port 13345"
        )
        assert ca.parse_parallelism(serve_args) == {"data_parallel_size": 2}


class TestPrecisionFromModel:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("org/Model-FP8", "fp8"),
            ("org/model-fp4-instruct", "fp4"),
            ("org/model-int4", "int4"),
            ("org/Model-FP16", "fp16"),
            # No marker: left unknown rather than guessed.
            ("org/Model-Instruct", ""),
        ],
    )
    def test_read_from_the_model_id(self, model, expected):
        assert ca.precision_from_model(model) == expected


class TestWorkloadEntry:
    def test_projects_recipe_fields_and_configs(self):
        entry, configs = ca.workload_entry(
            {
                "name": "minimax_m2_5-mi355x",
                "gpu": "MI355X",
                "vllm": {
                    "model": "org/Model-FP8",
                    "serve_args": "--tensor-parallel-size 8",
                },
                "vllm_bench": {
                    "configs": [
                        {
                            "name": "8k-in-1k-out",
                            "input_len": 8192,
                            "output_len": 1024,
                            "max_concurrency": 128,
                        }
                    ]
                },
            }
        )
        assert entry["device"] == "mi355x"
        assert entry["parallelism"] == {"tensor_parallel_size": 8}
        assert entry["precision"] == "fp8"
        # Keyed on the expanded run name. perf-eval suffixes every run with
        # -conc-<value>, even a single value, and the artifact is named after
        # the run — so keying on the bare name would miss on every lookup and
        # silently drop ISL/OSL from every result.
        assert configs["8k-in-1k-out-conc-128"] == {"isl": 8192, "osl": 1024, "conc": 128}
        assert "8k-in-1k-out" not in configs

    def test_metadata_overrides_inference(self):
        entry, _ = ca.workload_entry(
            {
                "name": "wl",
                "gpu": "MI355X",
                "vllm": {"model": "org/Model", "serve_args": "--tensor-parallel-size 8"},
                "vllm_bench": {"metadata": {"device": "mi300x", "tp": 4, "precision": "mxfp4"}},
            }
        )
        assert (entry["device"], entry["parallelism"], entry["precision"]) == (
            "mi300x",
            {"tensor_parallel_size": 4},
            "mxfp4",
        )

    def test_unnamed_configs_are_skipped(self):
        _, configs = ca.workload_entry(
            {"name": "wl", "gpu": "MI355X", "vllm_bench": {"configs": [{"input_len": 1}]}}
        )
        assert configs == {}

    def test_a_concurrency_sweep_expands_to_one_config_per_value(self):
        # Every AMD recipe sweeps concurrency, so this is the normal case, not
        # an edge case. Each swept run is a separate artifact.
        _, configs = ca.workload_entry(
            {
                "name": "wl-mi355x",
                "gpu": "MI355X",
                "vllm_bench": {
                    "configs": [
                        {
                            "name": "1k-in-1k-out",
                            "input_len": 1024,
                            "output_len": 1024,
                            "num_prompts": [10, 256, 512],
                            "max_concurrency": [1, 64, 128],
                        }
                    ]
                },
            }
        )
        assert set(configs) == {
            "1k-in-1k-out-conc-1",
            "1k-in-1k-out-conc-64",
            "1k-in-1k-out-conc-128",
        }
        # Shape is carried onto every swept run, not just the first.
        for run, config in configs.items():
            assert config["isl"] == 1024, run
            assert config["osl"] == 1024, run
        assert configs["1k-in-1k-out-conc-64"]["conc"] == 64

    def test_nightly_is_captured_for_the_expectation(self):
        entry, _ = ca.workload_entry({"name": "wl", "gpu": "MI355X", "nightly": True})
        assert entry["nightly"] is True
        entry, _ = ca.workload_entry({"name": "wl", "gpu": "MI355X"})
        assert entry["nightly"] is False

    def test_the_declared_accuracy_tasks_are_captured(self):
        entry, _ = ca.workload_entry(
            {
                "name": "wl",
                "gpu": "MI355X",
                "lm_eval": {"timeout": 6000, "tasks": [{"name": "gsm8k", "num_fewshot": 5}]},
            }
        )
        assert entry["accuracy_tasks"] == ["gsm8k"]


class TestLmEvalTasks:
    """The recipe's `lm_eval.tasks`, which name the accuracy results expected."""

    def test_task_names_are_read_from_the_mappings(self):
        recipe = {"lm_eval": {"tasks": [{"name": "gsm8k"}, {"name": "mmlu"}]}}
        assert ca.lm_eval_tasks(recipe) == ["gsm8k", "mmlu"]

    def test_a_recipe_without_lm_eval_expects_no_accuracy(self):
        assert ca.lm_eval_tasks({"name": "wl"}) == []
        assert ca.lm_eval_tasks({"lm_eval": None}) == []
        assert ca.lm_eval_tasks({"lm_eval": {"timeout": 60}}) == []

    def test_unnamed_and_malformed_tasks_are_skipped(self):
        recipe = {
            "lm_eval": {"tasks": [{"num_fewshot": 5}, {"name": "  "}, 7, None, {"name": "gsm8k"}]}
        }
        assert ca.lm_eval_tasks(recipe) == ["gsm8k"]

    def test_duplicates_collapse_and_order_is_stable(self):
        recipe = {"lm_eval": {"tasks": [{"name": "mmlu"}, {"name": "gsm8k"}, {"name": "mmlu"}]}}
        assert ca.lm_eval_tasks(recipe) == ["gsm8k", "mmlu"]


class TestExpectedAccuracy:
    """Accuracy coverage is measured against the recipes, like perf coverage.

    Inferring it from the last WINDOW_DAYS of results meant a workload whose
    lm-eval step had been failing for longer than the window dropped out of its
    own denominator, so the page stopped reporting it missing at exactly the
    point the outage became serious.
    """

    def _recipe(self, name, device, *, nightly=True, tasks=("gsm8k",)):
        return ca.workload_entry(
            {
                "name": name,
                "gpu": device.upper(),
                "nightly": nightly,
                "vllm": {"model": "org/Model-FP8", "serve_args": "--tensor-parallel-size 8"},
                "lm_eval": {"tasks": [{"name": t} for t in tasks]},
            }
        )

    def test_one_entry_per_declared_task(self):
        expected = ca.expected_accuracy(
            {"wl-mi355x": self._recipe("wl-mi355x", "mi355x", tasks=("gsm8k", "mmlu"))}
        )
        assert [e["task"] for e in expected] == ["gsm8k", "mmlu"]

    def test_carries_the_fields_coverage_matches_on(self):
        (expected,) = ca.expected_accuracy({"wl-mi355x": self._recipe("wl-mi355x", "mi355x")})
        assert expected == {
            "workload": "wl-mi355x",
            "model": "org/Model-FP8",
            "device": "mi355x",
            "task": "gsm8k",
        }

    def test_a_workload_without_lm_eval_expects_no_accuracy(self):
        recipes = {"wl-mi355x": self._recipe("wl-mi355x", "mi355x", tasks=())}
        assert ca.expected_accuracy(recipes) == []

    def test_it_shares_the_scope_filters_with_perf(self):
        non_nightly = {"wl-mi355x": self._recipe("wl-mi355x", "mi355x", nightly=False)}
        assert ca.expected_accuracy(non_nightly) == []
        nvidia = {"wl-h200": self._recipe("wl-h200", "h200")}
        assert ca.expected_accuracy(nvidia) == []

    def test_output_is_deterministic(self):
        recipes = {
            "b-mi355x": self._recipe("b-mi355x", "mi355x"),
            "a-mi300x": self._recipe("a-mi300x", "mi300x"),
        }
        assert ca.expected_accuracy(recipes) == ca.expected_accuracy(recipes)
        assert [e["workload"] for e in ca.expected_accuracy(recipes)][0] == "a-mi300x"

    def test_a_workload_broken_all_window_is_still_expected(self):
        # The regression this guards: expectation must not depend on results.
        recipes = {"wl-mi355x": self._recipe("wl-mi355x", "mi355x")}
        assert [e["task"] for e in ca.expected_accuracy(recipes)] == ["gsm8k"]


class TestExpectedConfigs:
    """Coverage is measured against the recipes, not against recent reporting.

    An expectation derived from recent data forgets whatever has been absent
    long enough, so the longer a workload stays broken the healthier the
    dashboard would claim to be. The recipes never decay.
    """

    def _recipe(self, name, device, *, nightly=True, concurrencies=(64, 128)):
        return ca.workload_entry(
            {
                "name": name,
                "gpu": device.upper(),
                "nightly": nightly,
                "vllm": {"model": "org/Model-FP8", "serve_args": "--tensor-parallel-size 8"},
                "vllm_bench": {
                    "configs": [
                        {
                            "name": "1k-in-1k-out",
                            "input_len": 1024,
                            "output_len": 1024,
                            "max_concurrency": list(concurrencies),
                        }
                    ]
                },
            }
        )

    def test_one_entry_per_expanded_run(self):
        expected = ca.expected_configs({"wl-mi355x": self._recipe("wl-mi355x", "mi355x")})
        assert len(expected) == 2
        assert {e["conc"] for e in expected} == {64, 128}

    def test_carries_the_fields_coverage_matches_on(self):
        expected = ca.expected_configs({"wl-mi355x": self._recipe("wl-mi355x", "mi355x")})
        for field in (
            "workload",
            "run",
            "model",
            "device",
            "precision",
            "parallelism",
            "isl",
            "osl",
            "conc",
        ):
            assert field in expected[0], field

    def test_non_nightly_recipes_are_not_expected(self):
        recipes = {"wl-mi355x": self._recipe("wl-mi355x", "mi355x", nightly=False)}
        assert ca.expected_configs(recipes) == []

    def test_nvidia_recipes_are_not_expected(self):
        recipes = {"wl-h200": self._recipe("wl-h200", "h200")}
        assert ca.expected_configs(recipes) == []

    def test_output_is_deterministic(self):
        recipes = {
            "b-mi355x": self._recipe("b-mi355x", "mi355x"),
            "a-mi300x": self._recipe("a-mi300x", "mi300x"),
        }
        assert ca.expected_configs(recipes) == ca.expected_configs(recipes)
        # Sorted by workload, so a diff of the published payload stays readable.
        assert [e["workload"] for e in ca.expected_configs(recipes)][0] == "a-mi300x"


class TestPerfEvent:
    def _identity(self):
        return {
            "build_number": 42,
            "build_url": "https://buildkite.com/vllm/perf-eval/builds/42",
            "build_commit": "f" * 40,
            "branch": "main",
            "vllm_commit": COMMIT,
            # Finished the day after the date it is named for.
            "date": "2026-07-01T04:00:00Z",
            "nightly_date": "2026-06-30",
            "image": f"vllm/vllm-openai-rocm:nightly-{COMMIT}",
        }

    def _entry(self, **overrides):
        entry = {
            "name": "wl-mi355x",
            "device": "mi355x",
            "tp": 4,
            "precision": "fp8",
            "model": "org/Model",
        }
        entry.update(overrides)
        return entry

    def test_canonical_perf_event(self):
        raw = {
            "model_id": "org/Model",
            "total_token_throughput": 800.0,
            "output_throughput": 200.0,
            "max_concurrency": 64,
        }
        event = ca.perf_event(
            raw,
            entry=self._entry(),
            config={"isl": 8192, "osl": 1024, "conc": 128},
            identity=self._identity(),
        )
        assert event is not None
        assert event["event"] == "perf_result"
        assert event["nightly"] is True
        assert event["metrics"]["tput_per_gpu"] == 200.0
        assert event["conc"] == 128
        assert event["vllm_commit"] == COMMIT
        assert event["nightly_date"] == "2026-06-30"

    def test_throughput_is_divided_by_every_gpu_the_server_uses(self):
        # vLLM's world size: TP x PP x PCP x DP. Expert parallel reuses them.
        parallelism = {
            "tensor_parallel_size": 2,
            "pipeline_parallel_size": 2,
            "data_parallel_size": 2,
            "enable_expert_parallel": True,
        }
        raw = {"total_token_throughput": 800.0, "output_throughput": 200.0}
        event = ca.perf_event(
            raw, entry=self._entry(parallelism=parallelism), config={}, identity=self._identity()
        )
        assert event is not None
        assert event["parallelism"] == parallelism
        assert event["metrics"]["tput_per_gpu"] == 100.0

    def test_request_counts_are_recorded(self):
        raw = {"total_token_throughput": 800.0, "completed": 500, "failed": 12}
        event = ca.perf_event(raw, entry=self._entry(), config={}, identity=self._identity())
        assert event is not None
        assert (event["completed_requests"], event["failed_requests"]) == (500.0, 12.0)

    def test_concurrency_falls_back_to_the_raw_result(self):
        event = ca.perf_event(
            {"total_token_throughput": 10.0, "max_concurrency": 64},
            entry=self._entry(),
            config={},
            identity=self._identity(),
        )
        assert event is not None
        assert event["conc"] == 64

    def test_nvidia_entry_is_dropped(self):
        identity = self._identity()
        identity["image"] = "vllm/vllm-openai:nightly"
        event = ca.perf_event(
            {"total_token_throughput": 10.0},
            entry=self._entry(name="wl-h200", device="h200"),
            config={},
            identity=identity,
        )
        assert event is None

    @pytest.mark.parametrize(
        "raw",
        [
            {"model_id": "org/Model"},  # nothing numeric at all
            {"error": "server crashed", "completed": 0},
            {"total_token_throughput": 0.0, "mean_ttft_ms": 120.0},
            {"total_token_throughput": float("nan")},
        ],
    )
    def test_a_failed_benchmark_is_skipped_not_published_as_zero(self, raw):
        # Zero throughput would read as a 100% regression tonight and a false
        # recovery tomorrow.
        event = ca.perf_event(raw, entry=self._entry(), config={}, identity=self._identity())
        assert event is None


class TestAccuracyEvent:
    def test_model_comes_from_the_recipe_not_lm_evals_backend(self):
        results = {
            "config": {
                "model": "local-completions",
                "model_args": "model=openai/gpt-oss-120b,base_url=http://x/v1/completions",
            },
            "results": {"gsm8k": {"sample_len": 1319, "exact_match,flexible-extract": 0.76}},
        }
        event = ca.accuracy_event(
            results,
            workload="gpt_oss_120b-mi355x",
            task="gsm8k",
            entry={"device": "mi355x", "model": "openai/gpt-oss-120b"},
            identity={
                "image": f"vllm/vllm-openai-rocm:nightly-{COMMIT}",
                "build_number": 1,
                "nightly_date": "2026-06-30",
            },
        )
        assert event is not None
        assert event["model"] == "openai/gpt-oss-120b"
        assert event["nightly_date"] == "2026-06-30"
        assert [row["metric"] for row in event["results"]] == ["exact_match,flexible-extract"]


class TestEventKey:
    def test_perf_key_separates_configs(self):
        base = {
            "event": "perf_result",
            "build_number": 1,
            "model": "m",
            "device": "mi355x",
            "isl": 1,
            "osl": 1,
        }
        assert ca.event_key({**base, "conc": 1}) != ca.event_key({**base, "conc": 2})

    def test_perf_key_separates_tp_variants_of_one_shape(self):
        base = {
            "event": "perf_result",
            "build_number": 1,
            "model": "m",
            "device": "mi355x",
            "isl": 1,
            "osl": 1,
            "conc": 1,
        }
        assert ca.event_key({**base, "tp": 4}) != ca.event_key({**base, "tp": 8})
        tp8 = {**base, "parallelism": {"tensor_parallel_size": 8}}
        tp4_dp2 = {**base, "parallelism": {"tensor_parallel_size": 4, "data_parallel_size": 2}}
        assert ca.event_key(tp8) != ca.event_key(tp4_dp2)
        # An event stored with only ``tp`` is the same result as its map form.
        assert ca.event_key(tp8) == ca.event_key({**base, "tp": 8})

    def test_accuracy_key_folds_task_rows(self):
        event = {
            "event": "accuracy_result",
            "build_number": 1,
            "workload": "wl",
            "results": [
                {"task": "gsm8k", "metric": "acc,none"},
                {"task": "gsm8k", "metric": "exact_match,strict-match"},
            ],
        }
        # Row order must not change the identity.
        reversed_event = {**event, "results": list(reversed(event["results"]))}
        assert ca.event_key(event) == ca.event_key(reversed_event)


class TestArtifactProvenance:
    def test_captures_stable_fields_and_normalizes_them(self):
        provenance = ca.artifact_provenance(
            {
                "id": "artifact-1",
                "job_id": "job-1",
                "path": "./results/wl/bench-a.json",
                "sha1sum": "ABCDEF",
                "download_url": "https://example.invalid/presigned?sig=secret",
            },
            42,
        )
        assert provenance["buildkite_artifact_path"] == "results/wl/bench-a.json"
        assert provenance["buildkite_artifact_sha1"] == "abcdef"

    def test_never_persists_a_presigned_download_url(self):
        provenance = ca.artifact_provenance(
            {"id": "a", "download_url": "https://example.invalid/presigned?sig=secret"}, 1
        )
        assert not any("sig=secret" in str(value) for value in provenance.values())
        assert "download_url" not in provenance


class TestLookbackGuard:
    @pytest.mark.parametrize("days", [0, -1, 31, 999])
    def test_out_of_range_lookback_is_rejected(self, days, tmp_path):
        with pytest.raises(ValueError, match="lookback must be between"):
            ca.collect(tmp_path / "events.jsonl", days=days, bk_token="t", gh_token="")


RECIPE_YAML = """\
name: {name}
gpu: MI355X
vllm:
  model: org/Model-FP8
  serve_args: --tensor-parallel-size 8
vllm_bench:
  configs:
    - name: 8k-in-1k-out
      input_len: 8192
      output_len: 1024
      max_concurrency: 128
"""


def recipe_archive(files: dict[str, str]) -> bytes:
    """A gzipped tarball shaped like GitHub's: everything under one top directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"vllm-project-perf-eval-abc1234/{path}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _ArchiveResponse:
    def __init__(self, body: bytes, status_code: int = 200):
        self._body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]


class TestRecipeArchive:
    @pytest.fixture
    def serve(self, monkeypatch):
        calls = []

        def install(body: bytes, status_code: int = 200):
            def fake_get(url, **kwargs):
                calls.append((url, kwargs))
                return _ArchiveResponse(body, status_code)

            monkeypatch.setattr(ca.requests, "get", fake_get)
            return calls

        return install

    def test_one_request_per_commit(self, serve):
        calls = serve(recipe_archive({f"workloads/wl{i}.yaml": "name: x\n" for i in range(28)}))
        texts = ca.fetch_workload_texts("", ref="abc1234")
        assert len(texts) == 28
        assert len(calls) == 1
        url, _ = calls[0]
        assert url.endswith(f"/repos/{ca.WORKLOAD_REPO}/tarball/abc1234")

    def test_reads_only_yaml_directly_under_workloads(self, serve):
        serve(
            recipe_archive(
                {
                    "workloads/a.yaml": "name: a\n",
                    "workloads/b.yml": "name: b\n",
                    "workloads/README.md": "not a recipe",
                    "workloads/archive/old.yaml": "name: old\n",
                    "configs/other.yaml": "name: other\n",
                }
            )
        )
        assert sorted(ca.fetch_workload_texts("", ref="abc1234")) == ["a.yaml", "b.yml"]

    def test_sends_the_token_only_when_there_is_one(self, serve):
        calls = serve(recipe_archive({}))
        ca.fetch_workload_texts("", ref="r")
        ca.fetch_workload_texts("gh-token", ref="r")
        assert "Authorization" not in calls[0][1]["headers"]
        assert calls[1][1]["headers"]["Authorization"] == "Bearer gh-token"

    def test_an_http_error_fails_rather_than_returning_no_recipes(self, serve):
        # No recipes would label nothing and quietly empty the dashboard.
        serve(b"", status_code=404)
        with pytest.raises(RuntimeError, match="404"):
            ca.fetch_workload_texts("", ref="missing")

    def test_an_oversized_archive_is_refused(self, serve, monkeypatch):
        monkeypatch.setattr(ca, "MAX_RECIPE_ARCHIVE_BYTES", 10)
        serve(recipe_archive({"workloads/a.yaml": "name: a\n"}))
        with pytest.raises(RuntimeError, match="exceeds"):
            ca.fetch_workload_texts("", ref="r")

    def test_the_workload_map_is_keyed_by_recipe_name(self, serve):
        serve(recipe_archive({"workloads/minimax.yaml": RECIPE_YAML.format(name="minimax-mi355x")}))
        workloads = ca.fetch_workload_map("", ref="abc1234")
        entry, configs = workloads["minimax-mi355x"]
        assert entry["device"] == "mi355x"
        assert configs["8k-in-1k-out-conc-128"] == {"isl": 8192, "osl": 1024, "conc": 128}


class TestRecheckBuildsArgument:
    @pytest.mark.parametrize("value", ["-1", str(ca.MAX_RECHECK_BUILDS + 1)])
    def test_out_of_range_is_rejected_before_any_request(self, monkeypatch, value):
        monkeypatch.setattr("sys.argv", ["collect_artifacts.py", "--recheck-builds", value])
        monkeypatch.setattr(ca, "collect", lambda *a, **k: pytest.fail("collect ran"))
        with pytest.raises(SystemExit):
            ca.main()


class TestDuplicateRecipeKeys:
    def test_each_duplicate_is_warned_about_once_per_run(self, monkeypatch, caplog):
        recipe = "name: wl-mi355x\nvllm_bench: {}\nvllm_bench: {}\n"
        monkeypatch.setattr(ca, "_warned_duplicate_keys", set())
        monkeypatch.setattr(ca, "fetch_workload_texts", lambda _token, ref: {"wl.yaml": recipe})
        with caplog.at_level("WARNING"):
            ca.fetch_workload_map("", ref="a" * 40)
            ca.fetch_workload_map("", ref="b" * 40)
        warnings = [r for r in caplog.records if "more than once" in r.getMessage()]
        assert len(warnings) == 1
        assert "aaaaaaaaaaaa" in warnings[0].getMessage()


class TestCheckpointPrecision:
    """What a checkpoint's config.json says vLLM loads."""

    @pytest.mark.parametrize(
        "config,expected",
        [
            (
                {"quantization_config": {"quant_method": "fp8"}, "torch_dtype": "bfloat16"},
                ("fp8", "bf16"),
            ),
            ({"quantization_config": {"quant_method": "mxfp4"}}, ("mxfp4", "")),
            (
                {
                    "quantization_config": {
                        "quant_method": "compressed-tensors",
                        "format": "pack-quantized",
                        "config_groups": {"group_0": {"weights": {"num_bits": 4, "type": "int"}}},
                    }
                },
                ("int4", ""),
            ),
            (
                {
                    "quantization_config": {
                        "quant_method": "compressed-tensors",
                        "format": "nvfp4-pack-quantized",
                    }
                },
                ("nvfp4", ""),
            ),
            (
                # amd/MiniMax-M3-MXFP4, as AMD Quark writes it.
                {
                    "quantization_config": {
                        "quant_method": "quark",
                        "global_quant_config": {
                            "weight": {"dtype": "fp4", "group_size": 32, "scale_format": "e8m0"}
                        },
                    },
                    "torch_dtype": "bfloat16",
                },
                ("mxfp4", "bf16"),
            ),
            (
                {
                    "quantization_config": {
                        "quant_method": "quark",
                        "global_quant_config": {"weight": {"dtype": "fp8_e4m3"}},
                    }
                },
                ("fp8", ""),
            ),
            ({"dtype": "bfloat16"}, ("", "bf16")),
            ({"torch_dtype": "float16"}, ("", "fp16")),
            # A multimodal checkpoint keeps its language model's settings in text_config.
            (
                {
                    "text_config": {
                        "quantization_config": {"quant_method": "fp8"},
                        "dtype": "bfloat16",
                    }
                },
                ("fp8", "bf16"),
            ),
            ({}, ("", "")),
        ],
    )
    def test_reads_the_quantization_and_dtype(self, config, expected):
        assert ca.checkpoint_precision(config) == expected


class TestRecipePrecision:
    FP8_CHECKPOINT = {"quantization_config": {"quant_method": "fp8"}, "torch_dtype": "bfloat16"}
    BF16_CHECKPOINT = {"torch_dtype": "bfloat16"}

    def test_the_recipes_own_precision_wins(self):
        assert (
            ca.recipe_precision({"precision": "mxfp4"}, "", "org/M", self.FP8_CHECKPOINT) == "mxfp4"
        )

    def test_quantization_flag_overrides_the_checkpoint(self):
        args = "--quantization deepseek_v4_fp8 --tensor-parallel-size 8"
        assert ca.recipe_precision({}, args, "org/M", self.BF16_CHECKPOINT) == "fp8"

    def test_a_quantized_checkpoint_beats_the_dtype_flag(self):
        # --dtype sets the compute dtype; quantized weights still load quantized.
        assert ca.recipe_precision({}, "--dtype float16", "org/M", self.FP8_CHECKPOINT) == "fp8"

    def test_the_dtype_flag_beats_the_checkpoint_dtype(self):
        assert ca.recipe_precision({}, "--dtype float16", "org/M", self.BF16_CHECKPOINT) == "fp16"

    def test_an_unquantized_checkpoint_runs_at_its_dtype(self):
        assert ca.recipe_precision({}, "", "openai/model", self.BF16_CHECKPOINT) == "bf16"

    def test_without_a_checkpoint_the_model_id_marker_is_used(self):
        assert ca.recipe_precision({}, "", "org/Model-FP8", None) == "fp8"

    def test_nothing_to_go_on_is_left_unstated(self):
        assert ca.recipe_precision({}, "", "org/Model", None) == ""


class TestFetchCheckpointConfig:
    class _Response:
        def __init__(self, body, status=200):
            self._body, self.status_code = body, status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise ca.requests.HTTPError(str(self.status_code))

        def json(self):
            return self._body

    def test_each_model_is_fetched_once_per_run(self, monkeypatch):
        calls = []

        def fake_get(url, **kwargs):
            calls.append(url)
            return self._Response({"torch_dtype": "bfloat16"})

        monkeypatch.setattr(ca, "_checkpoint_configs", {})
        monkeypatch.setattr(ca.requests, "get", fake_get)
        ca.fetch_checkpoint_config("org/Model")
        ca.fetch_checkpoint_config("org/Model")
        assert calls == ["https://huggingface.co/org/Model/resolve/main/config.json"]

    def test_a_gated_model_without_a_token_is_none(self, monkeypatch):
        monkeypatch.setattr(ca, "_checkpoint_configs", {})
        monkeypatch.delenv("HF_TOKEN", raising=False)
        monkeypatch.setattr(ca.requests, "get", lambda *a, **k: self._Response({}, 401))
        assert ca.fetch_checkpoint_config("org/Gated") is None

    def test_the_token_is_sent_when_set(self, monkeypatch):
        seen = {}

        def fake_get(url, headers=None, **kwargs):
            seen.update(headers or {})
            return self._Response({})

        monkeypatch.setattr(ca, "_checkpoint_configs", {})
        monkeypatch.setenv("HF_TOKEN", "hf_test")
        monkeypatch.setattr(ca.requests, "get", fake_get)
        ca.fetch_checkpoint_config("org/Gated")
        assert seen["Authorization"] == "Bearer hf_test"
