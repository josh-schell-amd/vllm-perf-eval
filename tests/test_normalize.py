"""Tests for the pure normalizers, including the AMD-only scope filter."""

from __future__ import annotations

import pytest

from perf_eval import normalize as nz


class TestAmdScopeFilter:
    @pytest.mark.parametrize("device", ["mi300x", "mi355x", "MI355X", " mi250 "])
    def test_amd_devices_match(self, device):
        assert nz.is_amd_device(device) is True
        assert nz.is_amd_workload(device=device) is True

    @pytest.mark.parametrize("device", ["h200", "b200", "a100", "", None])
    def test_nvidia_devices_excluded(self, device):
        assert nz.is_amd_device(device) is False
        assert nz.is_amd_workload(device=device) is False

    @pytest.mark.parametrize(
        "workload",
        ["minimax_m2_5_mi355x", "kimi_k3_mi355x", "deepseek_v4_pro-mi300x"],
    )
    def test_amd_workload_stems_match(self, workload):
        assert nz.is_amd_workload(workload=workload) is True

    @pytest.mark.parametrize("workload", ["gpt_oss_120b_h200", "glm_5_3_flash_b200"])
    def test_nvidia_workload_stems_excluded(self, workload):
        assert nz.is_amd_workload(workload=workload) is False

    def test_rocm_image_marks_amd(self):
        assert nz.is_amd_workload(image="vllm/vllm-openai-rocm:nightly-abc") is True
        assert nz.is_amd_workload(image="vllm/vllm-openai:nightly-abc") is False

    def test_any_single_amd_signal_is_enough(self):
        assert nz.is_amd_workload(workload="unknown", image="", device="mi355x") is True


class TestParallelism:
    @pytest.mark.parametrize(
        "parallelism,label,gpus",
        [
            ({}, "TP1", 1),
            ({"tensor_parallel_size": 8}, "TP8", 8),
            ({"tensor_parallel_size": 4, "data_parallel_size": 2}, "TP4×DP2", 8),
            ({"data_parallel_size": 8, "enable_expert_parallel": True}, "TP1×DP8 · EP", 8),
            # Decode context parallel splits the TP group, so it adds no GPUs.
            ({"tensor_parallel_size": 8, "decode_context_parallel_size": 2}, "TP8×DCP2", 8),
            ({"tensor_parallel_size": 2, "pipeline_parallel_size": 2}, "TP2×PP2", 4),
            (
                {"tensor_parallel_size": 8, "future_parallel_mode": "ring"},
                "TP8 · future_parallel_mode=ring",
                8,
            ),
        ],
    )
    def test_label_and_gpu_count(self, parallelism, label, gpus):
        assert nz.parallel_label(parallelism) == label
        assert nz.gpu_count(parallelism) == gpus


class TestNumericGuards:
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "x", None])
    def test_rejected(self, value):
        assert nz.to_float(value) is None

    def test_accepted(self):
        assert nz.to_float("1.5") == 1.5
        assert nz.to_int("7") == 7
        assert nz.to_int("x") is None


class TestAccuracyRows:
    def test_flattens_and_marks_primary(self):
        payload = {
            "data": {
                "results": {
                    "gsm8k": {
                        "exact_match,strict-match": 0.81,
                        "exact_match_stderr,strict-match": 0.01,
                        "alias": "gsm8k",
                    }
                }
            }
        }
        rows = nz.accuracy_rows(payload)
        assert rows == [
            {
                "task": "gsm8k",
                "metric": "exact_match,strict-match",
                "value": 0.81,
                "primary": True,
            }
        ]

    def test_without_a_preferred_metric_the_first_is_primary(self):
        payload = {"data": {"results": {"t": {"a": 0.1, "b": 0.2}}}}
        rows = nz.accuracy_rows(payload)
        assert [row["primary"] for row in rows] == [True, False]

    def test_sample_len_is_dropped_and_flexible_extract_leads(self):
        # The key order a real gsm8k artifact has: the question count first.
        payload = {
            "data": {
                "results": {
                    "gsm8k": {
                        "alias": "gsm8k",
                        "sample_len": 1319,
                        "exact_match,strict-match": 0.52,
                        "exact_match_stderr,strict-match": 0.01,
                        "exact_match,flexible-extract": 0.76,
                    }
                }
            }
        }
        rows = nz.accuracy_rows(payload)
        assert [(row["metric"], row["primary"]) for row in rows] == [
            ("exact_match,strict-match", False),
            ("exact_match,flexible-extract", True),
        ]


class TestMetricRegistry:
    def test_every_metric_declares_better_label_and_unit(self):
        for key, meta in nz.METRIC_META.items():
            assert meta["better"] in {"higher", "lower"}, key
            assert meta["label"], key
            assert "unit" in meta, key

    def test_throughput_is_higher_better_and_latency_lower_better(self):
        assert nz.METRIC_META["tput_per_gpu"]["better"] == "higher"
        assert nz.METRIC_META["mean_ttft"]["better"] == "lower"
        assert nz.METRIC_META["mean_tpot"]["better"] == "lower"
        assert nz.METRIC_META["mean_intvty"]["better"] == "higher"

    def test_accuracy_is_higher_better(self):
        assert nz.ACCURACY_BETTER == "higher"

    def test_per_gpu_throughput_says_so_in_its_unit(self):
        # transform_perf divides by TP, so these are TP times smaller than the
        # tok/s ATOM publishes for the same run. The page draws `unit` as the
        # axis title, so a bare "tok/s" would label the axis wrongly.
        for key in ("tput_per_gpu", "output_tput_per_gpu", "input_tput_per_gpu"):
            assert nz.METRIC_META[key]["unit"] == "tok/s/GPU", key

    def test_labels_name_the_metric_without_repeating_the_unit(self):
        # Label and unit are rendered side by side, so a unit in the label
        # shows up twice and can contradict the unit field.
        for key, meta in nz.METRIC_META.items():
            assert "tok/s" not in meta["label"], key
            assert meta["label"] != meta["unit"], key
