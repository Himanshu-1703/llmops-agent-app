"""Task Completion + Plan Quality + Plan Adherence + Tool Correctness eval for the hiring agent.

Every metric grades the SAME run: the agent runs once per golden, DeepEval's
CallbackHandler records one trace, and the trace metric reads the slim version of it.

    Task Completion                did the task get done?  (DeepEval, reads the trace)
    Plan Quality (sees workspace)  our G-Eval judge: task + plan + the snapshot the planner saw
    Plan Adherence (custom)        our G-Eval judge: task + plan + what the executor did per step
    Tool Correctness               DeepEval: the tool calls (recorded by ToolCallRecorder) vs the
                                   golden's expected_tools, exact match in code (no LLM)

DeepEval's built-in Plan Quality and Plan Adherence are not used: the first only sees the
task and the plan, the second is "adversarial" and marks correct runs down (see the
plan_quality_comparison report for the first).

Run (from the project root):
    uv run python -m hiring_agent.evals.eval_agent                 # all goldens
    uv run python -m hiring_agent.evals.eval_agent --only 2 6      # goldens number 2 and 6 (1-based)

Then open eval_results/reports/agent_eval_<date>_<time>.md.
"""

import argparse

from dotenv import load_dotenv

load_dotenv()

from deepeval.dataset import EvaluationDataset
from deepeval.evaluate.configs import AsyncConfig, DisplayConfig
from deepeval.integrations.langchain import CallbackHandler
from deepeval.test_case import LLMTestCase

from hiring_agent.evals.adherence_judge import adherence_test_case
from hiring_agent.evals.metrics import (
    ADHERENCE_JUDGE,
    JUDGE_MODEL,
    PLAN_QUALITY_JUDGE,
    TOOL_CORRECTNESS,
    make_plan_adherence_judge,
    make_plan_quality_judge,
    make_tool_correctness,
    make_trace_metrics,
)
from hiring_agent.evals.plan_judge import plan_test_case
from hiring_agent.evals.report import print_summary, write_report
from hiring_agent.evals.tool_calls import ToolCallRecorder, clean_tool_calls
from hiring_agent.app.clients import agent_params
from hiring_agent.app.agent import build_agent, run_agent
from hiring_agent.app.prompts import EXECUTOR_PROMPT_NAME, PLANNER_PROMPT_NAME, RESPONDER_PROMPT_NAME, load_prompt
from hiring_agent.app.workspace import ROOT_DIR, reset_workspace, workspace_snapshot

GOLDENS_PATH = ROOT_DIR / "goldens" / "goldens.json"


def load_goldens(only: list[int] | None):
    dataset = EvaluationDataset()
    dataset.add_goldens_from_json_file(str(GOLDENS_PATH))
    goldens = dataset.goldens
    if only:
        goldens = [goldens[n - 1] for n in only]  # 1-based, like the report
    return EvaluationDataset(goldens=goldens)


def run_metadata() -> dict:
    prompts = ", ".join(f"{n.split('_')[2]} v{load_prompt(n).version}" for n in (PLANNER_PROMPT_NAME, EXECUTOR_PROMPT_NAME, RESPONDER_PROMPT_NAME))
    return {
        "Planner model": agent_params.planner_model,
        "Executor model": agent_params.executor_model,
        "Responder model": agent_params.responder_model,
        "Judge model": JUDGE_MODEL,
        "Langfuse prompts": prompts,
    }


def run_eval(only: list[int] | None = None) -> tuple[list, str | None]:
    """Run the agent and every metric on the goldens, write the report.

    Returns (rows, report markdown path). The experiment pipeline calls this too.
    """
    dataset = load_goldens(only)
    agent = build_agent()
    meta = run_metadata()

    rows = []  # (golden, {metric name: metric}, final state); scores are filled in by DeepEval
    report_path = None
    try:
        for golden in dataset.evals_iterator(
            async_config=AsyncConfig(run_async=False),  # one golden at a time: they share one workspace/
            display_config=DisplayConfig(inspect_after_run=False),
        ):
            reset_workspace()
            snapshot = workspace_snapshot()  # exactly what the planner is about to see

            # fresh metrics for every golden: a metric stores its own score and reason
            trace_metrics = make_trace_metrics()
            plan_judge = make_plan_quality_judge()
            adherence_judge = make_plan_adherence_judge()
            tool_correctness = make_tool_correctness()
            metrics = {**trace_metrics, PLAN_QUALITY_JUDGE: plan_judge, ADHERENCE_JUDGE: adherence_judge,
                       TOOL_CORRECTNESS: tool_correctness}

            recorder = ToolCallRecorder()  # collects this golden's tool calls for Tool Correctness
            state = run_agent(agent, golden.input, callbacks=[CallbackHandler(metrics=list(trace_metrics.values())), recorder], full=True)
            rows.append((golden, metrics, state))

            # these read the final state or the recorded tool calls, not the trace
            tools_test_case = LLMTestCase(
                input=golden.input,
                actual_output=state["response"],
                tools_called=recorder.tool_calls,
                expected_tools=clean_tool_calls(golden.expected_tools),  # same cleaning on both sides
            )
            for judge, test_case in (
                (plan_judge, plan_test_case(golden.input, state["plan"], snapshot)),
                (adherence_judge, adherence_test_case(golden.input, state["plan"], state["past_steps"])),
                (tool_correctness, tools_test_case),
            ):
                try:
                    judge.measure(test_case)
                except Exception as e:  # show it in the report instead of crashing the run
                    judge.error = f"{type(e).__name__}: {e}"
    finally:
        # always write the report, even if the run crashed or was stopped
        if rows:
            print_summary(rows)
            report_path = write_report(rows, "agent_eval", meta)
            print("\nReport:", report_path)
    return rows, report_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=int, nargs="+", help="run only these golden numbers (1-based)")
    args = parser.parse_args()

    run_eval(args.only)
