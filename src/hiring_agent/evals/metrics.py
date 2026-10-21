"""The metrics used by the agent eval.

A metric object stores its own score and reason, so eval_agent.py asks for a FRESH set
for every golden. Trace metrics are wrapped with with_slim_trace so the judge reads the
slim trace (see slim_trace.py) instead of the ~50k-token raw one.
"""

from deepeval.metrics import TaskCompletionMetric, ToolCorrectnessMetric
from deepeval.test_case import ToolCallParams

from hiring_agent.app.workspace import ROOT_DIR
from hiring_agent.evals.adherence_judge import make_adherence_judge
from hiring_agent.evals.plan_judge import make_plan_judge
from hiring_agent.evals.slim_trace import with_slim_trace

JUDGE_MODEL = "gpt-5.4-mini"
THRESHOLD = 0.7

RESULTS_DIR = ROOT_DIR / "eval_results"
TRACES_DIR = RESULTS_DIR / "traces"
REPORTS_DIR = RESULTS_DIR / "reports"

SlimTaskCompletion = with_slim_trace(TaskCompletionMetric, save_dir=TRACES_DIR)

PLAN_QUALITY_JUDGE = "Plan Quality (sees workspace)"
ADHERENCE_JUDGE = "Plan Adherence (custom)"
TOOL_CORRECTNESS = "Tool Correctness"


def make_trace_metrics() -> dict:
    """Metrics that read the trace. They are attached to the CallbackHandler."""
    return {
        "Task Completion": SlimTaskCompletion(threshold=THRESHOLD, model=JUDGE_MODEL),
    }


def make_plan_quality_judge():
    """Our Plan Quality judge. It does not read the trace; eval_agent.py calls it directly."""
    return make_plan_judge(model=JUDGE_MODEL, threshold=THRESHOLD)


def make_plan_adherence_judge():
    """Our Plan Adherence judge. It reads the plan and the executor's step results, not the trace."""
    return make_adherence_judge(model=JUDGE_MODEL, threshold=THRESHOLD)


def make_tool_correctness():
    """DeepEval's Tool Correctness: the agent's tool calls vs the golden's expected_tools.

    Exact match, decided in code with no LLM: same tools, same order, same arguments
    -> 1, anything else -> 0. should_exact_match compares call by call, so the order is
    checked too.

    No available_tools are passed, so DeepEval skips its LLM tool-selection check. That
    check read our cleaned calls (no subject/body) and marked correct drafts down, and
    it made a deterministic metric noisy.
    """
    return ToolCorrectnessMetric(
        should_exact_match=True,
        evaluation_params=[ToolCallParams.INPUT_PARAMETERS],
        threshold=THRESHOLD,
    )
