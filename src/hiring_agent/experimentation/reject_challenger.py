"""Reject the current challenger run.

Run this when a gate test fails (CI does it automatically):

    uv run python -m hiring_agent.experimentation.reject_challenger

The challenger's `stage` tag is deleted (the run and its metrics stay in MLflow, just
untagged). The champion is left untouched.
"""

from hiring_agent.utils.mlflow_utils import CHALLENGER, get_run_by_stage, init_mlflow, reject_challenger

experiment_id = init_mlflow()

challenger = get_run_by_stage(CHALLENGER, experiment_id)
print(f"challenger : {challenger.info.run_id}  ({challenger.info.run_name})")

reject_challenger(experiment_id)
print(f"cleared stage tag from {challenger.info.run_id}")
