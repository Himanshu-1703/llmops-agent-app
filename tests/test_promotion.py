"""Promotion gate (majority vote): is the challenger good enough to replace the champion?

Passes only if BOTH hold:
    - no metric is more than 2 x noise below the champion (same bound as the regression gate)
    - at least 3 of the 4 metrics are better than or equal to the champion
Reads metrics already logged to MLflow; runs no agent and no LLM.

Run (from the project root):
    uv run pytest tests/test_promotion.py -v
"""

import json
from pathlib import Path

import pytest

from hiring_agent.utils.compute_noise_thresholds import THRESHOLDS_PATH
from hiring_agent.utils.mlflow_utils import CHALLENGER, CHAMPION, get_metrics_from_stage, init_mlflow

NOISE_MULTIPLIER = 2
MIN_METRICS_TO_PASS = 3


def load_thresholds(thresholds_path: Path) -> dict:
    with open(thresholds_path, "r") as file:
        return json.load(file)["noise_thresholds"]


experiment_id = init_mlflow()

latest_metrics = get_metrics_from_stage(CHALLENGER, experiment_id)
champion_metrics = get_metrics_from_stage(CHAMPION, experiment_id)
noise_thresholds = load_thresholds(THRESHOLDS_PATH)

champion_metrics_names = sorted(champion_metrics)
latest_metrics_names = sorted(latest_metrics)


def test_similar_metric_names():
    if champion_metrics_names != latest_metrics_names:
        pytest.exit(reason=f"Comparison metrics are different: champion {champion_metrics_names}, "
                           f"challenger {latest_metrics_names}")


def test_promotion_majority_vote():
    verdicts = []
    better_or_equal_count = 0
    tolerance_breaches = []

    for metric in latest_metrics_names:
        champion_value = champion_metrics[metric]
        latest_value = latest_metrics[metric]
        tolerance = NOISE_MULTIPLIER * noise_thresholds[metric]
        # metrics are logged rounded to 2 decimals; round the delta so 0.89 - 0.89 is exactly 0
        delta = round(latest_value - champion_value, 4)

        is_better_or_equal = delta >= 0
        is_within_tolerance = delta >= -tolerance

        if is_better_or_equal:
            better_or_equal_count += 1
        if not is_within_tolerance:
            tolerance_breaches.append(metric)

        status = "BETTER" if is_better_or_equal else "NOISE" if is_within_tolerance else "FAIL"
        verdicts.append(
            f"  [{status}] {metric!r}: challenger={latest_value:.4f} champion={champion_value:.4f} "
            f"delta={delta:+.4f} tolerance=+/-{tolerance:.4f}"
        )

    total = len(latest_metrics_names)
    detail = "\n".join(verdicts)
    print(f"\n{better_or_equal_count}/{total} metrics better than or equal to the champion\n{detail}")

    assert not tolerance_breaches, (
        f"Promotion gate failed: metric(s) {tolerance_breaches} regressed beyond their noise "
        f"tolerance.\nPer-metric detail:\n{detail}"
    )
    assert better_or_equal_count >= MIN_METRICS_TO_PASS, (
        f"Promotion gate failed: {better_or_equal_count}/{total} metrics better than or equal to the "
        f"champion (need >= {MIN_METRICS_TO_PASS}/{total}).\nPer-metric detail:\n{detail}"
    )
