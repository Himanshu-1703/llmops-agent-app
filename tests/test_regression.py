"""Regression gate: the challenger must not be clearly worse than the champion on ANY metric.

"Clearly worse" = more than 2 x noise below the champion (noise from thresholds.json).
Reads metrics already logged to MLflow; runs no agent and no LLM.

Run (from the project root):
    uv run pytest tests/test_regression.py -v
"""

import json
from pathlib import Path

import pytest

from hiring_agent.utils.compute_noise_thresholds import THRESHOLDS_PATH
from hiring_agent.utils.mlflow_utils import CHALLENGER, CHAMPION, get_metrics_from_stage, init_mlflow

MULTIPLIER = 2


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


@pytest.mark.parametrize(argnames="metric", argvalues=latest_metrics_names)
def test_regression_on_metrics(metric: str):
    if metric not in noise_thresholds:
        pytest.fail(f"No noise threshold for metric {metric!r}; thresholds.json and the eval metrics are out of sync")

    noise_threshold = MULTIPLIER * noise_thresholds[metric]
    champion_value = champion_metrics[metric]
    latest_value = latest_metrics[metric]

    lower_bound = champion_value - noise_threshold
    assert latest_value >= lower_bound, (
        f"Metric {metric!r} regressed: challenger={latest_value:.4f} < "
        f"champion={champion_value:.4f} - {MULTIPLIER} * noise = {lower_bound:.4f}"
    )
