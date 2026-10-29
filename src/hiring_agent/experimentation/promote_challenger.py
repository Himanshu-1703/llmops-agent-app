"""Promote the current challenger run to champion.

Run this after both gate tests pass (the last CI job):

    uv run python -m hiring_agent.experimentation.promote_challenger

The current champion is retagged `stage=archived`; the challenger is retagged
`stage=champion`. Needs exactly one run tagged `stage=champion` and exactly one tagged
`stage=challenger`.
"""

from hiring_agent.utils.mlflow_utils import CHALLENGER, CHAMPION, get_run_by_stage, init_mlflow, promote_challenger

experiment_id = init_mlflow()

champion = get_run_by_stage(CHAMPION, experiment_id)
challenger = get_run_by_stage(CHALLENGER, experiment_id)

print(f"current champion : {champion.info.run_id}  ({champion.info.run_name})")
print(f"challenger       : {challenger.info.run_id}  ({challenger.info.run_name})")

old_id, new_id = promote_challenger(experiment_id)
print(f"archived  {old_id}")
print(f"promoted  {new_id} -> {CHAMPION}")
