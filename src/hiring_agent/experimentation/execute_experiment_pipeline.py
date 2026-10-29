"""Experiment pipeline: one eval run = one MLflow run on DagsHub.

Each run logs:
    params     everything in params.yaml (flattened), the Langfuse prompt versions, the judge model
    metrics    the average score of every eval metric, rounded to 2 decimals (as in the report's Aggregate table)
    artifacts  golden_dataset/  reports/  traces/  prompts/  code/
    tag        stage = challenger (the CI gates compare it with the stage = champion run)

Only one challenger exists at a time: a leftover one (e.g. from a crashed CI run) is
un-tagged before the new run starts. The 3 noise runs (phase = noise_threshold) were logged
before this tag existed; they are the only runs with that tag.

Run (from the project root):
    uv run python -m hiring_agent.experimentation.execute_experiment_pipeline              # all goldens
    uv run python -m hiring_agent.experimentation.execute_experiment_pipeline --only 1 2   # goldens 1 and 2

DagsHub auth: set DAGSHUB_USER_TOKEN in .env, or log in through the browser on the first run.
"""

import argparse
import re
import time
from pathlib import Path

import mlflow

from hiring_agent.config.parameter_config import PARAMS_PATH, ROOT_DIR, params_config
from hiring_agent.app.prompts import EXECUTOR_PROMPT_NAME, PLANNER_PROMPT_NAME, RESPONDER_PROMPT_NAME, load_prompt
from hiring_agent.evals.eval_agent import GOLDENS_PATH, run_eval
from hiring_agent.evals.metrics import JUDGE_MODEL, TRACES_DIR
from hiring_agent.evals.report import metric_averages
from hiring_agent.utils.mlflow_utils import CHALLENGER, TRACKING_URI, demote_stale_challengers, init_mlflow, set_stage

# the folders whose code is saved with every run
CODE_DIRS = ["app", "config", "evals"]


def flatten_params(params_dict: dict) -> dict:
    """Flatten the nested params into one level: {"agent": {"k": v}} -> {"k": v}."""
    flat = {}
    for key, value in params_dict.items():
        items = flatten_params(value).items() if isinstance(value, dict) else [(key, value)]
        for flat_key, flat_value in items:
            # the section names are dropped, so two sections must not share a key
            if flat_key in flat:
                raise ValueError(f"Duplicate key found: {flat_key}")
            flat[flat_key] = flat_value
    return flat


def metric_key(name: str) -> str:
    """MLflow rejects '(' and ')' in metric names: "Plan Quality (sees workspace)" -> "plan_quality_sees_workspace"."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def prompt_versions_and_texts() -> tuple[dict, dict]:
    """The Langfuse version and the raw text of each system prompt the agent uses.

    A prompt that could not be fetched (local fallback) is logged as version "fallback",
    so a run that did not use the Langfuse prompts is easy to spot.
    """
    versions, texts = {}, {}
    for name in (PLANNER_PROMPT_NAME, EXECUTOR_PROMPT_NAME, RESPONDER_PROMPT_NAME):
        prompt = load_prompt(name)
        role = name.split("_")[2]  # hiring_agent_planner_prompt -> planner
        versions[f"{role}_prompt_version"] = "fallback" if prompt.is_fallback else prompt.version
        texts[name] = prompt.prompt
    return versions, texts


def return_code_files() -> list[Path]:
    """Every .py file of the agent, its config and the evals, plus params.yaml."""
    package_dir = ROOT_DIR / "src" / "hiring_agent"
    code_files = [path for folder in CODE_DIRS for path in sorted((package_dir / folder).glob("*.py"))]
    return code_files + [PARAMS_PATH]


def code_artifact_dir(code_file: Path) -> str:
    """Artifact folder of a code file: src/hiring_agent/app/agent.py -> code/src/hiring_agent/app."""
    return (Path("code") / code_file.parent.relative_to(ROOT_DIR)).as_posix()


def get_run_traces(since: float) -> list[Path]:
    """Trace files written during this run.

    Trace filenames have no timestamp (a new run overwrites the old file for the same
    task), so the files modified after the run started are exactly this run's traces.
    """
    return sorted(path for path in TRACES_DIR.glob("*.json") if path.stat().st_mtime >= since)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=int, nargs="+", help="run only these golden numbers (1-based)")
    args = parser.parse_args()

    experiment_id = init_mlflow()

    # this run becomes the new challenger, so clear the tag off any leftover one first
    stale = demote_stale_challengers(experiment_id)
    if stale:
        print(f"Cleared the {CHALLENGER} tag from {stale} leftover run(s)")

    with mlflow.start_run() as run:
        set_stage(run.info.run_id, CHALLENGER)

        # params: params.yaml + the prompt versions actually fetched + the judge model
        prompt_versions, prompt_texts = prompt_versions_and_texts()
        params_dict = flatten_params(params_config.model_dump())
        mlflow.log_params({**params_dict, **prompt_versions, "judge_model": JUDGE_MODEL})

        # run the eval (agent + metrics on every golden); it also writes the md + csv report
        start = time.time()
        rows, report_md = run_eval(args.only)

        # metrics: average score per eval metric, rounded to 2 decimals like the report
        metrics = {metric_key(name): round(avg, 2) for name, avg in metric_averages(rows).items()}
        mlflow.log_metrics(metrics)

        # artifacts
        mlflow.log_artifact(GOLDENS_PATH, "golden_dataset")
        mlflow.log_artifact(report_md, "reports")
        mlflow.log_artifact(Path(report_md).with_suffix(".csv"), "reports")
        for trace_file in get_run_traces(since=start):
            mlflow.log_artifact(trace_file, "traces")
        for name, text in prompt_texts.items():
            mlflow.log_text(text, artifact_file=f"prompts/{name}.txt")
        for code_file in return_code_files():
            # keep the project layout (code/src/hiring_agent/app/...) so same-named files don't overwrite each other
            mlflow.log_artifact(code_file, code_artifact_dir(code_file))

    print(f"\nMLflow run: {run.info.run_name} ({run.info.run_id})")
    print(f"{TRACKING_URI}/#/experiments/{run.info.experiment_id}/runs/{run.info.run_id}")
