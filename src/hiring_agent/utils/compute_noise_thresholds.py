"""Noise thresholds: how much each eval metric moves between repeated runs of the SAME config.

Reads every MLflow run tagged `phase=noise_threshold` (3 runs of planner v2 / executor v1 /
responder v1, Langfuse label `noise`), takes the sample standard deviation of each metric (numpy, ddof=1),
and writes it to thresholds.json at the project root. The CI gates (tests/) treat a drop of
up to 2 x this noise as "no real change".

Run (from the project root):
    uv run python -m hiring_agent.utils.compute_noise_thresholds
"""

import json

import numpy as np

from hiring_agent.config.parameter_config import ROOT_DIR
from hiring_agent.utils.mlflow_utils import NOISE_PHASE, get_runs_by_phase, init_mlflow

THRESHOLDS_PATH = ROOT_DIR / "thresholds.json"
PROMPT_VERSION_PARAMS = ["planner_prompt_version", "executor_prompt_version", "responder_prompt_version"]


def calculate_noise_thresholds(all_metrics: list[dict[str, float]]) -> dict[str, float]:
    """Sample standard deviation (ddof=1) of every metric across the runs."""
    if len(all_metrics) < 2:
        raise ValueError(f"Need at least 2 noise runs to measure noise, found {len(all_metrics)}")
    # every run must have logged the same metrics, or the std would mix different things
    metric_names = sorted(all_metrics[0])
    for metrics in all_metrics:
        if sorted(metrics) != metric_names:
            raise ValueError(f"Noise runs logged different metrics: {metric_names} vs {sorted(metrics)}")

    # one row per run, one column per metric; std down each column
    matrix = np.array([[metrics[name] for name in metric_names] for metrics in all_metrics])
    stds = np.std(matrix, axis=0, ddof=1).tolist()
    return dict(zip(metric_names, stds))


def print_table(runs: list, noise: dict[str, float]) -> None:
    """One line per run (prompt versions + metrics), then the noise (std) line."""
    names = list(noise)
    print(f"{'run':<22} {'P/E/R':<7} " + " ".join(f"{name:>28}" for name in names))
    for run in runs:
        versions = "/".join(run.data.params.get(param, "?") for param in PROMPT_VERSION_PARAMS)
        print(f"{run.info.run_name:<22} {versions:<7} " + " ".join(f"{run.data.metrics[name]:>28.4f}" for name in names))
    print(f"{'noise (std)':<22} {'':<7} " + " ".join(f"{noise[name]:>28.4f}" for name in names))


if __name__ == "__main__":
    experiment_id = init_mlflow()
    runs = get_runs_by_phase(NOISE_PHASE, experiment_id)

    noise_thresholds = calculate_noise_thresholds([run.data.metrics for run in runs])
    print_table(runs, noise_thresholds)

    with open(THRESHOLDS_PATH, "w") as file:
        json.dump({"noise_thresholds": noise_thresholds}, file, indent=4)
    print(f"\nSaved {len(noise_thresholds)} noise thresholds from {len(runs)} runs to {THRESHOLDS_PATH.name}")
