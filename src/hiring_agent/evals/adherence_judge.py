"""A Plan Adherence judge that checks the executor against the plan, without being harsh.

DeepEval's PlanAdherenceMetric is "adversarial": its prompt says to assume non-adherence and to
give the lowest score for ANY extra action, so correct runs often score 0 to 0.5. This judge
asks one question per plan step: was this step carried out as written?

It does not read the trace. The agent's final state already has everything it needs:
    input          the user's task
    context        the plan, numbered, exactly as the planner wrote it
    actual_output  for every step: what the executor reported doing (its result text)

Usage (see eval_agent.py):
    judge = make_adherence_judge(model=JUDGE_MODEL)
    judge.measure(adherence_test_case(task, state["plan"], state["past_steps"]))
    judge.score, judge.reason
"""

from deepeval.metrics import GEval
from deepeval.metrics.g_eval import Rubric
from deepeval.test_case import LLMTestCase, SingleTurnParams

GRADING_STEPS = [
    # What the judge has to work with
    "The input is the recruiter's task. The context is the agent's PLAN, numbered. The actual "
    "output is the EXECUTION LOG: for every plan step, what the executor reported doing. The "
    "executor runs one plan step at a time, sees only the earlier results and the current "
    "step, and makes at most one tool call per step. Judge ONLY plan adherence: was each step "
    "carried out as written? Do not judge whether the plan was good, whether the task was "
    "achieved, or whether the screening scores are right.",

    # Step by step
    "Go through the plan one step at a time. A step ADHERED if the log shows the action the "
    "step names: the tool it names, on the target it names, or for a step that says to make "
    "no tool call, no tool call. A step did NOT adhere if it is missing from the log, if a "
    "different tool, file or path was used than the step names, or if a path, name or ID the "
    "step says to copy from an earlier result was copied wrongly.",

    # Conditional steps
    "Conditional steps ('if the candidate in step 3 was shortlisted and has an email, call "
    "draft_email; otherwise make no tool call and say why') ADHERED when the executor took the "
    "branch that the earlier results require. Check the condition against the earlier results "
    "in the log. Taking the wrong branch, such as drafting an email for a rejected candidate "
    "or skipping it for a shortlisted one with an email, did NOT adhere.",

    # Order
    "Order: steps must be done in the plan's order. A step done before an earlier one, or "
    "repeated, did NOT adhere.",

    # Extras: only real extra actions
    "Extra actions: an EXTRA action is a tool call or change that no plan step asked for, such "
    "as moving, deleting, adding or drafting something the plan did not say to do. Nothing "
    "else counts as extra.",

    # What is NOT a deviation
    "These are NOT deviations, do not deduct for them: filling in values a step leaves open "
    "(a candidate's name, email, years, nice-to-haves, notes, the email subject and body); "
    "computing the fit score or decision with the scoring rule when the step says to apply it; "
    "copying a file's contents into the result; a short summary or extra explanation in the "
    "result text; saying why a step was skipped; the wording of the executor's message. A step "
    "that is worded loosely is adhered to if the log shows a reasonable reading of it.",

    # Fair, not strict
    "Be fair, not strict. If the log is unclear about a step, give the benefit of the doubt. "
    "Do not mark a step as not adhered because of small differences in wording. Only deduct "
    "for a real, clearly visible deviation, and name the step number and what happened.",

    # Score
    "Give the reason in 1 to 3 sentences, naming any step that did not adhere.",
]

# The score bands, as (score range 0-10, what the run looks like). GEval divides the score by 10.
RUBRICS = [
    ((10, 10), "Every step adhered exactly, there are no extra actions, and the log is clear for every step."),
    ((9, 9), "Every step adhered, with one step done a little loosely but clearly a reasonable reading of it."),
    ((8, 8), "Every step adhered, but two or more steps were done loosely, or the log is unclear about "
             "one step (give the benefit of the doubt)."),
    ((7, 7), "Every step adhered, but the log is unclear about several steps."),
    ((5, 6), "Exactly one step did not adhere (missing, wrong target, or a wrongly copied value), "
             "with no wrong branch and no extra action."),
    ((3, 4), "Two steps did not adhere, with no wrong branch and no extra action."),
    ((1, 2), "Any wrong branch of a conditional step, any extra action that changes the workspace, "
             "or three or more steps that did not adhere."),
    ((0, 0), "The plan was ignored: almost nothing in the log matches it."),
]


def execution_log(past_steps: list[tuple[str, str]]) -> str:
    """What the executor reported for each step, in order."""
    return "\n\n".join(
        f"STEP {n}: {step}\nEXECUTOR REPORTED: {result}" for n, (step, result) in enumerate(past_steps, 1)
    )


def adherence_test_case(task: str, plan: list[str], past_steps: list[tuple[str, str]]) -> LLMTestCase:
    """Package one run for the judge: the task, the numbered plan, and the execution log."""
    return LLMTestCase(
        input=task,
        actual_output=execution_log(past_steps),
        context=["PLAN:\n" + "\n".join(f"{n}. {step}" for n, step in enumerate(plan, 1))],
    )


def make_adherence_judge(model: str, threshold: float = 0.7) -> GEval:
    """A fresh judge for one golden (a metric stores its own score and reason)."""
    return GEval(
        name="Plan Adherence (custom)",
        evaluation_steps=GRADING_STEPS,
        rubric=[Rubric(score_range=score_range, expected_outcome=outcome) for score_range, outcome in RUBRICS],
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.CONTEXT,
            SingleTurnParams.ACTUAL_OUTPUT,
        ],
        model=model,
        threshold=threshold,
        async_mode=False,  # we call measure() ourselves inside the eval loop
    )
