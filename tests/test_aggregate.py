"""Tests for aggregation, the scope filter, and series-ordering correctness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import accuracy_result, days_ago, perf_result, received_days_ago
from perf_eval import aggregate as agg


def _only_model(payload):
    assert len(payload["models"]) == 1
    return payload["models"][0]


def _metric(payload, key="tput_per_gpu"):
    model = _only_model(payload)
    assert len(model["perf_configs"]) == 1
    return model["perf_configs"][0]["metrics"][key]


class TestScopeFilter:
    def test_nvidia_results_are_excluded(self):
        events = [
            perf_result(device="h200", model="nvidia-model"),
            perf_result(device="mi355x", model="amd-model"),
        ]
        payload = agg.aggregate(events)
        assert [m["model"] for m in payload["models"]] == ["amd-model"]
        assert payload["summary"]["amd_devices"] == ["mi355x"]

    def test_non_nightly_results_are_excluded(self):
        events = [perf_result(nightly=False)]
        assert agg.aggregate(events)["models"] == []

    def test_nightly_flag_must_be_exactly_true(self):
        events = [perf_result(nightly="yes")]
        assert agg.aggregate(events)["models"] == []

    def test_non_result_events_are_ignored(self):
        events = [{"event": "build", "nightly": True}, perf_result()]
        assert len(agg.aggregate(events)["models"]) == 1

    def test_scope_is_declared_in_the_payload(self):
        payload = agg.aggregate([perf_result()])
        assert payload["scope"]["hardware"] == "amd"
        assert payload["scope"]["runs"] == "nightly"
        assert "NVIDIA" in payload["scope"]["description"]


class TestSeriesOrdering:
    def test_newest_observation_wins_regardless_of_store_order(self):
        # Same nightly (same commit) observed twice. The newer observation
        # appears FIRST in the store, so resolving by list position would pick
        # the stale value.
        events = [
            perf_result(commit="a" * 40, value=100.0, date="2026-01-02 00:00:00"),
            perf_result(commit="a" * 40, value=50.0, date="2026-01-01 00:00:00"),
        ]
        block = _metric(agg.aggregate(events))
        assert block["series"][-1]["value"] == 100.0
        assert len(block["series"]) == 1

    def test_newest_observation_wins_when_store_order_agrees(self):
        events = [
            perf_result(commit="a" * 40, value=50.0, date="2026-01-01 00:00:00"),
            perf_result(commit="a" * 40, value=100.0, date="2026-01-02 00:00:00"),
        ]
        block = _metric(agg.aggregate(events))
        assert block["series"][-1]["value"] == 100.0

    def test_retried_nightly_on_the_same_commit_is_one_point(self):
        # A nightly re-run produces a second build number for one commit. That
        # is one nightly, so it must not appear twice in the trend line.
        events = [
            perf_result(commit="a" * 40, build_number=1, value=100.0),
            perf_result(commit="a" * 40, build_number=2, value=110.0),
        ]
        block = _metric(agg.aggregate(events))
        assert len(block["series"]) == 1

    def test_distinct_nightlies_are_distinct_points_sorted_oldest_first(self):
        events = [
            perf_result(commit="b" * 40, value=200.0, date="2026-01-03 00:00:00"),
            perf_result(commit="a" * 40, value=100.0, date="2026-01-01 00:00:00"),
        ]
        block = _metric(agg.aggregate(events))
        assert [point["value"] for point in block["series"]] == [100.0, 200.0]

    def test_unparseable_date_is_logged_not_crashed(self, caplog):
        event = perf_result(date="not-a-date")
        with caplog.at_level("WARNING"):
            payload = agg.aggregate([event])
        assert len(payload["models"]) == 1
        assert "no parseable date" in caplog.text


class TestPublishedSeries:
    """The page compares nightlies itself, so the payload carries series only."""

    def test_a_metric_publishes_its_series_without_a_verdict(self):
        events = [
            perf_result(commit="a" * 40, value=100.0, date="2026-01-01 00:00:00"),
            perf_result(commit="b" * 40, value=103.0, date="2026-01-02 00:00:00"),
        ]
        block = _metric(agg.aggregate(events))
        assert set(block) == {"better", "label", "series", "unit"}
        assert block["better"] == "higher"
        assert [point["value"] for point in block["series"]] == [100.0, 103.0]

    def test_lower_is_better_is_recorded_on_the_metric(self):
        events = [
            perf_result(commit="a" * 40, metrics={"mean_ttft": 0.10}, date="2026-01-01 00:00:00"),
            perf_result(commit="b" * 40, metrics={"mean_ttft": 0.05}, date="2026-01-02 00:00:00"),
        ]
        block = _metric(agg.aggregate(events), "mean_ttft")
        assert block["better"] == "lower"
        assert "status" not in block

    def test_a_first_nightly_is_a_one_point_series(self):
        block = _metric(agg.aggregate([perf_result()]))
        assert len(block["series"]) == 1
        assert "previous" not in block

    def test_an_accuracy_task_publishes_its_series_without_a_verdict(self):
        events = [
            accuracy_result(commit="a" * 40, value=0.80, date="2026-01-01 00:00:00"),
            accuracy_result(commit="b" * 40, value=0.81, date="2026-01-02 00:00:00"),
        ]
        task = _only_model(agg.aggregate(events))["accuracy_tasks"][0]
        assert "status" not in task
        assert [point["value"] for point in task["series"]] == [0.80, 0.81]

    def test_thresholds_are_published_for_the_frontend(self):
        payload = agg.aggregate([perf_result()])
        assert payload["thresholds"] == {"perf_rel": 0.005, "accuracy_abs": 0.01}

    def test_the_thresholds_are_pinned(self):
        # Pinned so changing a threshold is a deliberate, visible change rather
        # than a quiet constant edit.
        assert agg.PERF_REL_THRESHOLD == 0.005
        assert agg.ACCURACY_ABS_THRESHOLD == 0.01


class TestGrouping:
    def test_configs_are_keyed_by_device_shape_and_concurrency(self):
        events = [perf_result(conc=128), perf_result(conc=256)]
        model = _only_model(agg.aggregate(events))
        assert [config["conc"] for config in model["perf_configs"]] == [128, 256]

    @staticmethod
    def _labels(payload: dict) -> list[tuple]:
        return sorted(
            (c["parallel_label"], c["gpus"], c["metrics"]["tput_per_gpu"]["series"][-1]["value"])
            for c in _only_model(payload)["perf_configs"]
        )

    def test_parallelism_variants_of_one_shape_are_separate_configs(self):
        events = [
            perf_result(parallelism={"tensor_parallel_size": 8}, value=60.0),
            perf_result(
                parallelism={"tensor_parallel_size": 4, "data_parallel_size": 2}, value=70.0
            ),
            perf_result(
                parallelism={"tensor_parallel_size": 8, "enable_expert_parallel": True}, value=80.0
            ),
        ]
        assert self._labels(agg.aggregate(events)) == [
            ("TP4×DP2", 8, 70.0),
            ("TP8", 8, 60.0),
            ("TP8 · EP", 8, 80.0),
        ]

    @staticmethod
    def _legacy(**kwargs) -> dict:
        """A perf event stored before the parallelism map, with only ``tp``."""
        event = perf_result(**kwargs)
        del event["parallelism"]
        event["tp"] = 8
        return event

    def test_an_event_with_only_tp_joins_its_series(self):
        old = self._legacy(commit="a" * 40, date=days_ago(2), value=50.0)
        new = perf_result(commit="b" * 40, date=days_ago(1), value=60.0)
        model = _only_model(agg.aggregate([old, new]))
        assert [len(c["metrics"]["tput_per_gpu"]["series"]) for c in model["perf_configs"]] == [2]

    def test_an_event_with_only_tp_takes_its_recipes_expert_parallelism(self):
        # Otherwise an EP recipe's older nightlies sit on a line of their own.
        ep = {"tensor_parallel_size": 8, "enable_expert_parallel": True}
        snapshot = {
            "event": "expected_configs",
            "received_at": received_days_ago(0),
            "configs": [
                {
                    "model": "meta-llama/Test-8B",
                    "device": "mi355x",
                    "precision": "fp8",
                    "parallelism": ep,
                    "isl": 1024,
                    "osl": 1024,
                    "conc": 128,
                }
            ],
        }
        old = self._legacy(commit="a" * 40, date=days_ago(2), value=50.0)
        new = perf_result(commit="b" * 40, date=days_ago(1), value=60.0, parallelism=ep)
        model = _only_model(agg.aggregate([snapshot, old, new]))
        assert [
            (c["parallel_label"], len(c["metrics"]["tput_per_gpu"]["series"]))
            for c in model["perf_configs"]
        ] == [("TP8 · EP", 2)]

    def test_a_shape_two_recipes_share_is_not_guessed(self):
        shared = {"model": "meta-llama/Test-8B", "device": "mi355x", "precision": "fp8"}
        shape = {"isl": 1024, "osl": 1024, "conc": 128}
        snapshot = {
            "event": "expected_configs",
            "received_at": received_days_ago(0),
            "configs": [
                {**shared, **shape, "parallelism": {"tensor_parallel_size": 8}},
                {
                    **shared,
                    **shape,
                    "parallelism": {"tensor_parallel_size": 8, "enable_expert_parallel": True},
                },
            ],
        }
        model = _only_model(agg.aggregate([snapshot, self._legacy()]))
        assert [c["parallel_label"] for c in model["perf_configs"]] == ["TP8"]

    def test_config_label_abbreviates_power_of_two_lengths(self):
        events = [perf_result(isl=8192, osl=1024, conc=128, device="mi355x")]
        model = _only_model(agg.aggregate(events))
        assert model["perf_configs"][0]["label"] == "8K in / 1K out @ conc 128 (MI355X)"

    def test_workload_set_is_discovered_not_hard_coded(self):
        events = [perf_result(model="brand-new/Model-1T")]
        assert _only_model(agg.aggregate(events))["model"] == ("brand-new/Model-1T")

    def test_missing_model_gets_a_placeholder_rather_than_being_dropped(self):
        events = [perf_result(model="")]
        assert _only_model(agg.aggregate(events))["model"] == ("(unknown model)")

    def test_each_point_names_a_build_whose_provenance_is_listed_once(self):
        payload = agg.aggregate([perf_result()])
        point = _metric(payload)["series"][0]
        assert set(point) == {"build", "value", "completed_requests", "failed_requests"}
        build = payload["builds"][point["build"]]
        for field in ("date", "nightly_date", "vllm_commit", "build_commit", "image", "build_url"):
            assert field in build

    def test_only_builds_a_point_names_are_listed(self):
        # A retried nightly: one point, from the newer build.
        events = [
            perf_result(commit="a" * 40, build_number=1, value=100.0),
            perf_result(commit="a" * 40, build_number=2, value=110.0),
        ]
        payload = agg.aggregate(events)
        assert list(payload["builds"]) == [_metric(payload)["series"][0]["build"]]

    def test_failed_requests_are_kept_on_every_series_point(self):
        event = perf_result()
        event.update(completed_requests=500.0, failed_requests=12.0)
        point = _metric(agg.aggregate([event]))["series"][0]
        assert (point["completed_requests"], point["failed_requests"]) == (500.0, 12.0)

    def test_summary_counts_points_and_nightlies(self):
        events = [
            perf_result(commit="a" * 40, date="2026-01-01 00:00:00"),
            perf_result(commit="b" * 40, date="2026-01-02 00:00:00"),
            accuracy_result(commit="a" * 40, date="2026-01-01 00:00:00"),
        ]
        summary = agg.aggregate(events)["summary"]
        assert summary["models"] == 1
        assert summary["nightlies"] == 2
        assert summary["perf_points"] == 2
        assert summary["accuracy_points"] == 1

    def test_primary_accuracy_tasks_sort_first(self):
        events = [
            accuracy_result(task="b_task", metric="acc,none"),
            accuracy_result(task="a_task", metric="acc,none"),
        ]
        events[0]["results"][0]["primary"] = False
        tasks = _only_model(agg.aggregate(events))["accuracy_tasks"]
        assert tasks[0]["primary"] is True


class TestAccuracyGrouping:
    def test_one_model_on_two_devices_is_two_series(self):
        events = [
            accuracy_result(device="mi300x", value=0.92),
            accuracy_result(device="mi355x", value=0.94),
        ]
        tasks = _only_model(agg.aggregate(events))["accuracy_tasks"]
        assert sorted((t["device"], t["series"][0]["value"]) for t in tasks) == [
            ("mi300x", 0.92),
            ("mi355x", 0.94),
        ]

    def test_sample_len_is_not_a_score(self):
        event = accuracy_result(metric="sample_len", value=1319.0)
        event["results"].append(
            {"task": "gsm8k", "metric": "exact_match,strict-match", "value": 0.9, "primary": False}
        )
        tasks = _only_model(agg.aggregate([event]))["accuracy_tasks"]
        assert [(t["metric"], t["primary"]) for t in tasks] == [("exact_match,strict-match", True)]

    def test_two_workloads_for_one_model_and_device_are_separate_series(self):
        events = [
            accuracy_result(workload="x_tp4-mi355x", value=0.9),
            accuracy_result(workload="x_tp8-mi355x", value=0.5),
        ]
        tasks = _only_model(agg.aggregate(events))["accuracy_tasks"]
        assert sorted((t["workload"], t["series"][0]["value"]) for t in tasks) == [
            ("x_tp4-mi355x", 0.9),
            ("x_tp8-mi355x", 0.5),
        ]


class TestWhatCountsAsANightly:
    """The nightly count is whatever `nightly_identity` considers one run:
    the named day and the vLLM commit, falling back to build number.
    """

    def _events(self, days: int, runs_per_day: int, *, distinct_commits: bool):
        events = []
        for day in range(days):
            for run in range(runs_per_day):
                index = day * runs_per_day + run if distinct_commits else day
                events.append(
                    perf_result(
                        commit=f"{index:040x}",
                        date=f"2026-01-{day + 1:02d} {run:02d}:00:00",
                        build_number=1000 + day * runs_per_day + run,
                    )
                )
        return events

    def test_several_runs_a_day_on_distinct_commits_count_separately(self):
        # 5 runs a day for 14 days on different commits is 70 nightlies.
        payload = agg.aggregate(self._events(14, 5, distinct_commits=True))
        assert payload["summary"]["nightlies"] == 70

    def test_several_runs_a_day_on_one_commit_collapse(self):
        # The same commit re-run 5 times a night is still one nightly: that is
        # the retry dedupe, not data loss.
        payload = agg.aggregate(self._events(14, 5, distinct_commits=False))
        assert payload["summary"]["nightlies"] == 14

    def test_one_run_a_day_is_one_nightly_a_day(self):
        # The real pipeline shape: one scheduled nightly per new vLLM commit.
        payload = agg.aggregate(self._events(14, 1, distinct_commits=True))
        assert payload["summary"]["nightlies"] == 14

    def test_a_run_without_a_commit_falls_back_to_build_number(self):
        events = [perf_result(commit="", build_number=n) for n in (1, 2, 3)]
        for event in events:
            event["vllm_commit"] = ""
            event["build_commit"] = ""
        assert agg.aggregate(events)["summary"]["nightlies"] == 3


class TestBuildPayload:
    """The payload publishes the nightlies from the last WINDOW_DAYS."""

    def _nights(self, ages: list[float]):
        return [
            perf_result(commit=f"{index:040x}", date=days_ago(age), build_number=1000 + index)
            for index, age in enumerate(ages)
        ]

    def test_only_nightlies_inside_the_window_are_published(self):
        w = agg.WINDOW_DAYS
        payload = agg.build_payload(self._nights([1, 5, w - 0.5, w + 0.5, w + 15]))
        assert len(_metric(payload)["series"]) == 3

    def test_nothing_in_the_window_publishes_no_models(self):
        payload = agg.build_payload(self._nights([agg.WINDOW_DAYS + 10, agg.WINDOW_DAYS + 1]))
        assert payload["models"] == []

    def test_the_window_is_published_so_the_page_cannot_drift(self):
        payload = agg.build_payload([perf_result()])
        assert payload["retention"] == {"display_window_days": agg.WINDOW_DAYS}


class TestExpectedIsPublished:
    """The coverage card needs the recipe-derived expectation in the payload.

    The page cannot reach GitHub, so the collector snapshots it into the store
    and the aggregator republishes the newest snapshot.
    """

    # `configs` is typed loosely on purpose: the store holds whatever JSON
    # arrived, and one test feeds it malformed entries to prove they are
    # dropped rather than published.
    def _snapshot(self, received_at: str, configs: list, accuracy: list | None = None) -> dict:
        return {
            "event": "expected_configs",
            "received_at": received_at,
            "configs": configs,
            "accuracy": accuracy if accuracy is not None else [],
        }

    def test_absent_snapshot_publishes_an_empty_expectation(self):
        payload = agg.aggregate([perf_result()])
        assert payload["expected"] == {"recorded_at": "", "configs": [], "accuracy": []}

    def test_the_accuracy_expectation_is_published(self):
        # Accuracy coverage reads this instead of inferring expectation from
        # the window, where a long outage erased its own denominator.
        accuracy = [
            {"workload": "wl-mi355x", "model": "org/M", "device": "mi355x", "task": "gsm8k"}
        ]
        snapshot = self._snapshot("2026-01-05T00:00:00Z", [], accuracy)
        assert agg.aggregate([snapshot])["expected"]["accuracy"] == accuracy

    def test_a_snapshot_predating_accuracy_publishes_an_empty_list(self):
        # Written before the collector recorded lm-eval tasks; the page falls
        # back to observed groups rather than reading undefined.
        snapshot = {
            "event": "expected_configs",
            "received_at": "2026-01-05T00:00:00Z",
            "configs": [{"workload": "wl"}],
        }
        assert agg.aggregate([snapshot])["expected"]["accuracy"] == []

    def test_malformed_accuracy_entries_are_dropped(self):
        snapshot = self._snapshot("2026-01-05T00:00:00Z", [], [{"task": "gsm8k"}, "nonsense", 7])
        assert agg.aggregate([snapshot])["expected"]["accuracy"] == [{"task": "gsm8k"}]

    def test_the_snapshot_is_published(self):
        configs = [
            {
                "workload": "wl-mi355x",
                "device": "mi355x",
                "conc": 64,
                "parallelism": {"tensor_parallel_size": 4, "enable_expert_parallel": True},
            }
        ]
        payload = agg.aggregate([perf_result(), self._snapshot("2026-01-05T00:00:00Z", configs)])
        # Labelled as results are, since coverage matches the two on the label.
        assert payload["expected"]["configs"] == [
            {**configs[0], "parallel_label": "TP4 · EP", "gpus": 4}
        ]
        assert payload["expected"]["recorded_at"] == "2026-01-05T00:00:00Z"

    def test_a_snapshot_predating_parallelism_is_labelled_from_its_tp(self):
        snapshot = self._snapshot("2026-01-05T00:00:00Z", [{"workload": "wl", "tp": 8}])
        config = agg.aggregate([snapshot])["expected"]["configs"][0]
        assert (config["parallel_label"], config["gpus"]) == ("TP8", 8)

    def test_the_newest_snapshot_wins(self):
        old = self._snapshot("2026-01-01T00:00:00Z", [{"workload": "old"}])
        new = self._snapshot("2026-02-01T00:00:00Z", [{"workload": "new"}])
        # Listed oldest-last to prove order in the store does not decide it.
        payload = agg.aggregate([new, old])
        assert [c["workload"] for c in payload["expected"]["configs"]] == ["new"]

    def test_malformed_entries_are_dropped_rather_than_published(self):
        snapshot = self._snapshot("2026-01-05T00:00:00Z", [{"workload": "ok"}, "nonsense", 7])
        payload = agg.aggregate([snapshot])
        assert [c["workload"] for c in payload["expected"]["configs"]] == ["ok"]

    def test_the_snapshot_is_not_mistaken_for_a_result(self):
        snapshot = self._snapshot("2026-01-05T00:00:00Z", [{"workload": "wl"}])
        payload = agg.aggregate([snapshot])
        assert payload["models"] == []
        assert payload["summary"]["nightlies"] == 0


class TestMetricDisplayMetadata:
    @pytest.fixture
    def metric_meta(self):
        return agg.aggregate([perf_result()])["metric_meta"]

    def test_display_order_is_explicit(self, metric_meta):
        # The published JSON is key-sorted, so insertion order cannot survive
        # the round trip and the order has to be carried as a field.
        orders = [meta["order"] for meta in metric_meta.values()]
        assert len(orders) == len(set(orders))
        assert min(orders) == 0

    def test_headline_throughput_metrics_come_first(self, metric_meta):
        assert metric_meta["tput_per_gpu"]["order"] == 0
        assert metric_meta["output_tput_per_gpu"]["order"] == 1
        # Secondary percentiles sort after the headline set.
        assert metric_meta["median_tpot"]["order"] > metric_meta["mean_ttft"]["order"]

    def test_latency_metrics_declare_a_display_unit_and_scale(self, metric_meta):
        # A chart axis has to pick one unit for every point, so the conversion
        # is per metric rather than per value.
        ttft = metric_meta["mean_ttft"]
        assert ttft["unit"] == "s"
        assert ttft["display_unit"] == "ms"
        assert ttft["display_scale"] == 1000

    def test_derived_metrics_are_not_counted(self, metric_meta):
        # Input throughput is total - output and interactivity is 1 / TPOT, so
        # counting them would count one move twice.
        uncounted = {
            k for k, meta in metric_meta.items() if k != "accuracy" and not meta["counted"]
        }
        assert uncounted == {"input_tput_per_gpu", "mean_intvty"}

    def test_throughput_metrics_need_no_conversion(self, metric_meta):
        assert "display_scale" not in metric_meta["tput_per_gpu"]

    def test_every_metric_declares_digits(self, metric_meta):
        for key, meta in metric_meta.items():
            assert isinstance(meta.get("digits"), int), key

    def test_accuracy_is_included_with_an_order(self, metric_meta):
        assert metric_meta["accuracy"]["better"] == "higher"
        assert "order" in metric_meta["accuracy"]


class TestGeneratedAt:
    def test_is_utc_iso_with_a_trailing_z(self):
        stamp = agg.aggregate([perf_result()])["generated_at"]
        parsed = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        assert abs(datetime.now(UTC) - parsed) < timedelta(minutes=5)

    def test_pipeline_provenance_is_published(self):
        payload = agg.aggregate([perf_result()])
        assert payload["pipeline"]["org"] == "vllm"
        assert payload["pipeline"]["slug"] == "perf-eval"


class TestPayloadSanityChecks:
    """What the collect workflow checks before persisting or deploying."""

    def test_a_built_payload_passes(self):
        assert agg.payload_problems(agg.build_payload([perf_result()])) == []
        assert agg.payload_problems(agg.build_payload([])) == []

    def test_a_missing_key_is_named(self):
        payload = agg.build_payload([perf_result()])
        del payload["metric_meta"]
        assert agg.payload_problems(payload) == ["missing metric_meta"]

    def test_models_must_be_a_list(self):
        payload = {**agg.build_payload([]), "models": {}}
        assert agg.payload_problems(payload) == ["models is not a list"]

    def test_counted_perf_points_with_no_models_is_caught(self):
        payload = agg.build_payload([perf_result()])
        payload["models"] = []
        assert agg.payload_problems(payload) == ["perf points counted but no models published"]

    def test_a_result_without_a_build_number_is_caught(self):
        event = perf_result()
        del event["build_number"]
        payload = agg.build_payload([event])
        assert agg.payload_problems(payload) == ["points name builds with no provenance: ['None']"]

    def test_a_failing_payload_is_not_written(self, monkeypatch, tmp_path):
        output = tmp_path / "perf_eval.json"
        monkeypatch.setattr(agg, "build_payload", lambda _events: {"models": []})
        monkeypatch.setattr(
            "sys.argv",
            ["aggregate.py", "--store", str(tmp_path / "events.jsonl"), "--output", str(output)],
        )
        assert agg.main() == 1
        assert not output.exists()


class TestRunSummary:
    def test_the_counts_are_summarized(self):
        text = agg.summary_markdown(agg.build_payload([perf_result()]))
        assert text.startswith("### Perf Eval collection\n")
        assert "- Perf points: " in text
        assert "Deploy skipped" not in text

    def test_a_skipped_deploy_is_noted(self):
        text = agg.summary_markdown(agg.build_payload([]), deploy_skipped=True)
        assert "- Deploy skipped: no new results since the last publish." in text

    def test_a_missing_payload_is_reported(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(
            "sys.argv", ["aggregate.py", "--summarize", str(tmp_path / "missing.json")]
        )
        assert agg.main() == 0
        assert "- No payload was produced." in capsys.readouterr().out


class TestNightlyRuns:
    """Every nightly the collector saw, so the page can explain a gap."""

    def _run(self, build: int, day: str, state: str = "passed") -> dict:
        return {
            "event": "nightly_run",
            "build_number": build,
            "nightly_date": day,
            "date": day + "T14:00:00Z",
            "state": state,
            "build_url": f"https://buildkite.com/vllm/perf-eval/builds/{build}",
            "received_at": "2026-01-10T00:00:00Z",
        }

    def test_each_run_carries_how_many_amd_results_it_produced(self):
        events = [
            perf_result(build_number=601),
            self._run(601, "2026-01-05"),
            self._run(603, "2026-01-06", state="failed"),
        ]
        runs = agg.aggregate(events)["nightly_runs"]
        assert [(r["build"], r["amd_results"], r["state"]) for r in runs] == [
            ("603", 0, "failed"),
            ("601", 1, "passed"),
        ]

    def test_newest_first_by_buildkite_date_then_build(self):
        events = [
            self._run(700, "2026-01-04"),
            self._run(702, "2026-01-06"),
            self._run(701, "2026-01-06"),
        ]
        assert [r["build"] for r in agg.aggregate(events)["nightly_runs"]] == ["702", "701", "700"]


class TestJobLinks:
    def test_a_config_names_the_job_that_ran_it_in_each_build(self):
        event = {**perf_result(build_number=601), "buildkite_artifact_job_id": "job-601"}
        (config,) = _only_model(agg.aggregate([event]))["perf_configs"]
        assert config["jobs"] == {"601": "job-601"}

    def test_an_accuracy_task_names_its_job_too(self):
        event = {**accuracy_result(build_number=601), "buildkite_artifact_job_id": "job-a"}
        (task, *_) = _only_model(agg.aggregate([event]))["accuracy_tasks"]
        assert task["jobs"] == {"601": "job-a"}
