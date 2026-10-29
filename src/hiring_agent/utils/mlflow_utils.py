"""MLflow helpers shared by the experiment pipeline, the noise script, the CI gates and the tag scripts.

Champion / challenger tag scheme (same as the RAG project): one tag key `stage`.
    champion    the best run so far (the one every new run is compared with)
    challenger  the new run under test in CI
    archived    a former champion
Rules: exactly one champion, at most one challenger.

The noise runs (repeats of one config, used to measure noise) are tagged `phase=noise_threshold` instead.
"""

import dagshub
import mlflow
from dotenv import load_dotenv
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

# DAGSHUB_USER_TOKEN from .env locally; in CI it comes from the GitHub secrets
load_dotenv()

REPO_OWNER = "himanshu1703"
REPO_NAME = "llmops-agent-app"
TRACKING_URI = "https://dagshub.com/himanshu1703/llmops-agent-app.mlflow"
EXPERIMENT_NAME = "hiring-agent"

NOISE_PHASE = "noise_threshold"
STAGE_TAG = "stage"
CHAMPION = "champion"
CHALLENGER = "challenger"
ARCHIVED = "archived"


def init_mlflow() -> str:
    """Point MLflow at the DagsHub server and return the experiment id (DAGSHUB_USER_TOKEN is read from the env)."""
    dagshub.init(repo_owner=REPO_OWNER, repo_name=REPO_NAME, mlflow=True)
    mlflow.set_tracking_uri(TRACKING_URI)
    return mlflow.set_experiment(EXPERIMENT_NAME).experiment_id


def get_runs_by_phase(phase: str, experiment_id: str) -> list:
    """Every run tagged `phase=<phase>` (the noise runs)."""
    return mlflow.search_runs(experiment_ids=[experiment_id],
                              filter_string=f"tags.phase = '{phase}'",
                              output_format="list")


def find_runs_by_stage(stage_name: str, experiment_id: str) -> list:
    """Every run currently tagged `stage=<stage_name>`."""
    return mlflow.search_runs(experiment_ids=[experiment_id],
                              filter_string=f"tags.{STAGE_TAG} = '{stage_name}'",
                              output_format="list")


def get_run_by_stage(stage_name: str, experiment_id: str):
    """The one run tagged `stage=<stage_name>`; raises unless exactly one exists."""
    runs = find_runs_by_stage(stage_name, experiment_id)
    if len(runs) != 1:
        raise RuntimeError(
            f"Expected exactly 1 run tagged {STAGE_TAG}={stage_name!r}, found {len(runs)}. "
            f"Fix the tags in the MLflow UI (tag a champion, or clear stray challengers)."
        )
    return runs[0]


def get_metrics_from_stage(stage_name: str, experiment_id: str) -> dict:
    return get_run_by_stage(stage_name, experiment_id).data.metrics


def set_stage(run_id: str, stage_name: str) -> None:
    MlflowClient().set_tag(run_id, STAGE_TAG, stage_name)


def clear_stage(run_id: str) -> None:
    """Remove the `stage` tag from a run; a missing tag is not an error."""
    try:
        MlflowClient().delete_tag(run_id, STAGE_TAG)
    except MlflowException:
        pass


def demote_stale_challengers(experiment_id: str) -> int:
    """Before a new run: clear the tag off any leftover challenger (e.g. a crashed CI run). Returns how many."""
    stale = find_runs_by_stage(CHALLENGER, experiment_id)
    for run in stale:
        clear_stage(run.info.run_id)
    return len(stale)


def promote_challenger(experiment_id: str) -> tuple[str, str]:
    """Swap the tags: champion -> archived, challenger -> champion. Returns (old_id, new_id)."""
    champion = get_run_by_stage(CHAMPION, experiment_id)
    challenger = get_run_by_stage(CHALLENGER, experiment_id)

    set_stage(champion.info.run_id, ARCHIVED)
    set_stage(challenger.info.run_id, CHAMPION)

    # check the swap left exactly one champion
    remaining = find_runs_by_stage(CHAMPION, experiment_id)
    if len(remaining) != 1:
        raise RuntimeError(
            f"After promotion {len(remaining)} runs are tagged {STAGE_TAG}={CHAMPION!r}. "
            f"Check the MLflow UI now."
        )
    return champion.info.run_id, challenger.info.run_id


def reject_challenger(experiment_id: str) -> str:
    """Clear the challenger's `stage` tag (the run and its metrics stay); the champion is untouched."""
    challenger = get_run_by_stage(CHALLENGER, experiment_id)
    clear_stage(challenger.info.run_id)
    return challenger.info.run_id


if __name__ == "__main__":
    # quick look at the current tags: python -m hiring_agent.utils.mlflow_utils
    experiment_id = init_mlflow()
    for run in get_runs_by_phase(NOISE_PHASE, experiment_id):
        print(f"{NOISE_PHASE:<11} {run.info.run_name}  ({run.info.run_id})")
    for stage in (CHAMPION, CHALLENGER, ARCHIVED):
        for run in find_runs_by_stage(stage, experiment_id):
            print(f"{stage:<11} {run.info.run_name}  ({run.info.run_id})")
