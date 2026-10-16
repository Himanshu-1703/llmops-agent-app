"""A Plan Quality judge that sees what the planner saw.

DeepEval's PlanQualityMetric grades a plan from the task and the plan text only. It never
sees the workspace or the tools, so it cannot tell a complete plan from an incomplete one
("screen every resume" looks incomplete unless you know which resumes exist). The snapshot
cannot be added through the trace either: its scoring prompt only receives task + plan.

So this is a DeepEval G-Eval metric with our own grading steps. It gets:
    input          the user's task
    actual_output  the plan, word for word, as the planner wrote it
    context        the workspace snapshot the planner was given, the tool list, and the
                   resume files the plan never mentions (worked out in code, exact)

Usage (see eval_agent.py):
    judge = make_plan_judge(model=JUDGE_MODEL)
    judge.measure(plan_test_case(task, plan_steps, snapshot))
    judge.score, judge.reason
"""

from deepeval.metrics import GEval
from deepeval.metrics.g_eval import Rubric
from deepeval.test_case import LLMTestCase, SingleTurnParams

from hiring_agent.app.tools import TOOL_LIST

GRADING_STEPS = [
    # What the judge has to work with
    "The input is the recruiter's task. The actual output is the agent's plan, one numbered "
    "step per line. The context holds (1) the workspace snapshot the planner was given: every "
    "file path that existed when the plan was written, plus the current tracker rows, but NOT "
    "file contents; (2) the tools the agent can use, with their arguments and what each one "
    "does; and (3) the files the plan never mentions. Each step is run later by an executor "
    "that makes at most one tool call per step and sees only the completed steps. After the "
    "plan runs, a separate responder always writes the final answer, so the plan does not "
    "need a step just to report results.",

    # Conditional steps are the normal way to plan here
    "Conditional steps: the planner cannot know what the resumes say, so it must not decide "
    "outcomes in advance. Steps such as 'call add_candidate for the candidate in step 2, "
    "applying the scoring rule from step 1' or 'if the candidate in step 3 was shortlisted "
    "and has an email, call draft_email; otherwise make no tool call and say why' are CORRECT. "
    "Deduct if the plan states a candidate's decision, score or skills before reading the "
    "resume, or invents file contents. A step such as 'Make no tool call: for the candidate in "
    "step 2, using the job description from step 1, state each must-have as met or not met, "
    "the matched nice-to-haves, the ATS score and the decision' is the CORRECT way to screen: "
    "it is an instruction to work the values out from the resume after it is read, followed by "
    "a short add_candidate step that uses them. It does NOT decide anything in advance.",

    # Completeness, checked against the snapshot, file by file
    "Completeness: the context has an exact, code-computed list of FILES THE PLAN NEVER "
    "MENTIONS. Go through it one file at a time and decide whether this task needs it. For "
    "'all / every / each' tasks every matching resume is needed (the executor cannot loop, "
    "so a step like 'for each resume' is a flaw). A needed file on that list is SKIPPED: a "
    "serious flaw, and you must name it in your reason. Trust that list: never say the plan "
    "skips nothing if a needed file is on it. Do not deduct for leaving out files the task "
    "does not need.",

    # Real names only
    "Names: every path the plan uses must appear in the snapshot, or be a folder/file the task "
    "asks to create. Using a name that does not exist is a serious flaw. If the task names "
    "something that is not in the snapshot or needs a tool the agent does not have (sending "
    "email, scheduling), the correct plan has a step that tells the user it cannot be done and "
    "why; that is not a missing step.",

    # Tools used for what they do
    "Tools: each tool call must use a tool for what its description says, with the argument "
    "names shown. Using a tool for something it cannot do (for example delete_file on a "
    "folder, or draft_email for a candidate with no email) is a serious flaw.",

    # Order and dependencies
    "Dependencies: job_description.md must be read before anyone is screened. A step that "
    "needs a file's contents or an earlier result must come after the step that produces it "
    "and refer to it. If a step needs the tracker, the plan must read candidates.csv after the "
    "add_candidate/update_candidate steps whose results it needs. A resume that is moved "
    "before its decision exists is a serious flaw.",

    # Screening criteria
    "Fairness: the plan must screen only on the job description's skills and experience. "
    "Deduct if it uses age, gender, marital status or other personal attributes.",

    # Efficiency
    "Efficiency: penalise steps that are not needed, such as reading a file whose contents "
    "do not matter or repeating the same call. A short plan that is complete is ideal.",

    # What NOT to penalise (what a context-blind judge keeps asking for)
    "Do not deduct for missing existence checks or for not calling list_files: the planner "
    "already has the snapshot. Do not deduct for missing error handling, for not verifying "
    "results, or for not having a separate step for small work the executor (an LLM) does "
    "naturally, such as applying the scoring rule or counting names in a list.",
]

# The score bands, as (score range 0-10, what the plan looks like). GEval divides the score by 10.
RUBRICS = [
    ((10, 10), "Ideal: complete for the task, only names from the snapshot, every tool used for what it does, "
               "steps in a sensible order, and no needless step. A one-step plan that reads the one file "
               "the task needs and reports from it is a 10. So is a screening plan that reads the job "
               "description, reads the resume, works the values out in a no-tool step, then adds the candidate."),
    ((9, 9), "Complete and correct, with one trivial looseness in wording that would not change what the "
             "executor does."),
    ((8, 8), "Complete and correct, but with one needless or repeated step (for example reading "
             "candidates.csv when no later step uses it) or one step that is vague."),
    ((7, 7), "Complete and correct, but with two minor weaknesses (needless, repeated or vague steps). "
             "The plan would still run correctly as written."),
    ((5, 6), "One real flaw that could give a wrong result: a resume the task needs is skipped, a step "
             "comes before the step it depends on, a tool is used for something it cannot do, or a "
             "step packs two actions together."),
    ((3, 4), "Two real flaws, or one flaw that would clearly break the task, such as several needed "
             "resumes skipped."),
    ((1, 2), "A serious flaw: it invents a file or name, decides an outcome before reading, or screens on "
             "personal attributes."),
    ((0, 0), "Unusable: empty, unrelated to the task, or it could not run at all."),
]


def files_in_snapshot(snapshot: str) -> list[str]:
    """File paths listed in workspace_snapshot() text (empty-folder markers are skipped)."""
    files = []
    for line in snapshot.splitlines():
        if line.startswith("TRACKER"):
            break
        path = line.strip()
        if line.startswith("  ") and path and not path.endswith("(empty)"):
            files.append(path)
    return files


def files_not_in_plan(plan: list[str], snapshot: str) -> list[str]:
    """Files the plan never names, worked out in code.

    A judge model is bad at comparing a long file list with a long plan line by line, so
    code does it exactly and the judge decides which of these files the task needs.
    """
    text = " ".join(plan)
    # only resumes: job_description.md and candidates.csv are read when a step needs them
    # (the tracker rows are already in the snapshot), so leaving them out is never a skipped file
    return [path for path in files_in_snapshot(snapshot) if path.startswith("resumes/") and path not in text]


def plan_test_case(task: str, plan: list[str], snapshot: str) -> LLMTestCase:
    """Package one run for the judge: the task, the exact plan, and what the planner saw."""
    untouched = files_not_in_plan(plan, snapshot)
    return LLMTestCase(
        input=task,
        actual_output="\n".join(f"{n}. {step}" for n, step in enumerate(plan, 1)),
        context=[
            "WORKSPACE SNAPSHOT (when the plan was written):\n" + snapshot,
            "FILES THE PLAN NEVER MENTIONS (worked out by code, exact; many of these are "
            "rightly left alone, so decide which ones this task needs):\n"
            + ("\n".join(untouched) if untouched else "(none)"),
            "TOOLS:\n" + TOOL_LIST,
        ],
    )


def make_plan_judge(model: str, threshold: float = 0.7) -> GEval:
    """A fresh judge for one golden (a metric stores its own score and reason)."""
    return GEval(
        name="Plan Quality (sees workspace)",
        evaluation_steps=GRADING_STEPS,
        rubric=[Rubric(score_range=score_range, expected_outcome=outcome) for score_range, outcome in RUBRICS],
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.CONTEXT,
        ],
        model=model,
        threshold=threshold,
        async_mode=False,  # we call measure() ourselves inside the eval loop
    )
