"""Online evals: grade the app's real tasks in Langfuse with our two plan judges.

This script runs on its own server, every 15 minutes (cron). Each run:
    1. finds the new `hiring-agent-run` traces since the last run
    2. reads the task, the plan, the step results, the workspace snapshot and the tool list
       from each trace
    3. grades the trace with Plan Quality (sees workspace) and Plan Adherence (custom)
    4. writes both scores onto the trace in Langfuse

Both judges need no correct answer (no golden), so they can grade real tasks. Tool
Correctness needs a golden, and Task Completion needs DeepEval's own trace format, so
neither is used here.

The script needs only deepeval, langfuse and dotenv, never the app's code, so it can run on
a small server. That is why the two judges are copied into it from evals/plan_judge.py and
evals/adherence_judge.py. If you change a judge there, change it here too.

Run (from the project root):
    uv run python src/hiring_agent/evals/online_evals/run_online_eval.py
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
# Langfuse quietly throws scores away when tracing is off, and the local .env turns it off.
# This script only writes scores, so it turns tracing on for itself (before get_client()).
os.environ["LANGFUSE_TRACING_ENABLED"] = "true"

from deepeval.metrics import GEval
from deepeval.metrics.g_eval import Rubric
from deepeval.test_case import LLMTestCase, SingleTurnParams
from langfuse import get_client

JUDGE_MODEL = "gpt-5.4-mini"  # same as evals/metrics.py
THRESHOLD = 0.7
TRACE_NAME = "hiring-agent-run"  # the root span's name, set in api/routers/agent.py
ENVIRONMENT = "production"  # "development" grades the traces of the API on your computer
LOOKBACK_MINUTES = 15  # how far back the very first run looks

# What the user sees when a guardrail blocks a task (app/guards.py). A blocked task has no plan.
GENERIC_BLOCK_MESSAGE = "This request cannot be processed. Please try again."

# Remembers when the last run happened, so each run only grades newer traces
CHECKPOINT_PATH = Path(__file__).resolve().parent / "eval_checkpoint.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("online_eval")


# --------------------------------------------------------------------------
# Judge 1: Plan Quality (sees workspace)   (copied from evals/plan_judge.py)
# --------------------------------------------------------------------------

PLAN_GRADING_STEPS = [
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
PLAN_RUBRICS = [
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
    """The file paths in the snapshot text (empty folders are left out)."""
    files = []
    for line in snapshot.splitlines():
        if line.startswith("TRACKER"):
            break
        path = line.strip()
        if line.startswith("  ") and path and not path.endswith("(empty)"):
            files.append(path)
    return files


def files_not_in_plan(plan: list[str], snapshot: str) -> list[str]:
    """The resumes the plan never names. Code finds these exactly; a judge often misses one."""
    text = " ".join(plan)
    return [path for path in files_in_snapshot(snapshot) if path.startswith("resumes/") and path not in text]


def plan_test_case(task: str, plan: list[str], snapshot: str, tool_list: str) -> LLMTestCase:
    """What the judge reads: the task, the numbered plan, and what the planner saw."""
    untouched = files_not_in_plan(plan, snapshot)
    return LLMTestCase(
        input=task,
        actual_output="\n".join(f"{n}. {step}" for n, step in enumerate(plan, 1)),
        context=[
            "WORKSPACE SNAPSHOT (when the plan was written):\n" + snapshot,
            "FILES THE PLAN NEVER MENTIONS (worked out by code, exact; many of these are "
            "rightly left alone, so decide which ones this task needs):\n"
            + ("\n".join(untouched) if untouched else "(none)"),
            "TOOLS:\n" + tool_list,
        ],
    )


def make_plan_judge() -> GEval:
    return GEval(
        name="Plan Quality (sees workspace)",
        evaluation_steps=PLAN_GRADING_STEPS,
        rubric=[Rubric(score_range=score_range, expected_outcome=outcome) for score_range, outcome in PLAN_RUBRICS],
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT, SingleTurnParams.CONTEXT],
        model=JUDGE_MODEL,
        threshold=THRESHOLD,
        async_mode=False,
    )


# --------------------------------------------------------------------------
# Judge 2: Plan Adherence (custom)   (copied from evals/adherence_judge.py)
# --------------------------------------------------------------------------

ADHERENCE_GRADING_STEPS = [
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

ADHERENCE_RUBRICS = [
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


def adherence_test_case(task: str, plan: list[str], past_steps: list) -> LLMTestCase:
    """What the judge reads: the task, the numbered plan, and what the executor did per step."""
    log = "\n\n".join(
        f"STEP {n}: {step}\nEXECUTOR REPORTED: {result}" for n, (step, result) in enumerate(past_steps, 1)
    )
    return LLMTestCase(
        input=task,
        actual_output=log,
        context=["PLAN:\n" + "\n".join(f"{n}. {step}" for n, step in enumerate(plan, 1))],
    )


def make_adherence_judge() -> GEval:
    return GEval(
        name="Plan Adherence (custom)",
        evaluation_steps=ADHERENCE_GRADING_STEPS,
        rubric=[Rubric(score_range=score_range, expected_outcome=outcome) for score_range, outcome in ADHERENCE_RUBRICS],
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.CONTEXT, SingleTurnParams.ACTUAL_OUTPUT],
        model=JUDGE_MODEL,
        threshold=THRESHOLD,
        async_mode=False,
    )


# --------------------------------------------------------------------------
# The checkpoint: when the last run happened
# --------------------------------------------------------------------------

def load_checkpoint() -> datetime:
    """The time of the last run. The very first run looks back LOOKBACK_MINUTES instead."""
    try:
        return datetime.fromisoformat(json.loads(CHECKPOINT_PATH.read_text())["last_processed_timestamp"])
    except (OSError, ValueError, KeyError):  # no file yet, or a broken one
        return datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)


def save_checkpoint(run_time: datetime) -> None:
    CHECKPOINT_PATH.write_text(json.dumps({"last_processed_timestamp": run_time.isoformat()}))


# --------------------------------------------------------------------------
# Reading a trace from Langfuse
# --------------------------------------------------------------------------

def fetch_new_trace_ids(client, since: datetime, until: datetime) -> list[str]:
    """The ids of the traces that started between `since` and `until`."""
    trace_ids, cursor = [], None
    while True:  # Langfuse returns at most 100 per page; `cursor` asks for the next page
        page = client.api.observations.get_many(
            name=TRACE_NAME, environment=ENVIRONMENT, from_start_time=since, to_start_time=until,
            fields="core", limit=100, cursor=cursor,
        )
        trace_ids += [span.trace_id for span in page.data]
        cursor = page.meta.cursor
        if not cursor:
            return trace_ids


def fetch_observations(client, trace_id: str) -> list:
    """Every step of one trace: spans, LLM calls, tool calls (a full screening run has 300+)."""
    observations, cursor = [], None
    while True:
        page = client.api.observations.get_many(trace_id=trace_id, fields="core,basic,io", limit=100, cursor=cursor)
        observations += page.data
        cursor = page.meta.cursor
        if not cursor:
            return observations


def as_data(value):
    """Langfuse gives some inputs as JSON text and some as dicts or lists. Always return the data."""
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return value


def text_between(text: str, start: str, end: str) -> str | None:
    """The text between two markers, or None if a marker is missing."""
    if start not in text:
        return None
    after = text.split(start, 1)[1]
    return after.split(end, 1)[0].strip() if end in after else None


def planner_messages(observations: list) -> tuple[str, str] | None:
    """The (system, user) messages of the planner's LLM call, or None if it is not found.

    The planner's call is the only LLM call whose user message holds the workspace snapshot.
    """
    for o in observations:
        if o.type != "GENERATION":
            continue
        messages = as_data(o.input) or []
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        user = next((m.get("content", "") for m in messages if m.get("role") in ("user", "human")), "")
        if "WORKSPACE SNAPSHOT:" in user:
            return system, user
    return None


def extract_run(observations: list) -> dict | str:
    """Everything the two judges need from one trace, or the reason the trace is skipped."""
    root = next((o for o in observations if o.name == TRACE_NAME), None)
    if root is None:
        return "no root span"
    if root.output == GENERIC_BLOCK_MESSAGE:
        return "blocked by a guardrail (no plan)"

    # the responder runs last, so its input holds the final plan and every step's result
    responder = next((o for o in observations if o.name == "responder"), None)
    if responder is None:
        return "no responder step (the run crashed or was blocked)"
    state = as_data(responder.input)
    plan, past_steps = state.get("plan") or [], state.get("past_steps") or []
    if not plan:
        return "empty plan"

    messages = planner_messages(observations)
    if messages is None:
        return "planner LLM call not found"
    system, user = messages
    # If a new planner prompt drops these headings, skip the trace instead of grading it blind
    snapshot = text_between(user, "WORKSPACE SNAPSHOT:", "\n\nTASK:")
    tool_list = text_between(system, "TOOLS:", "\n\nRULES:")
    if snapshot is None or tool_list is None:
        return "snapshot or tool list not found in the planner prompt"

    return {"task": as_data(root.input), "plan": plan, "past_steps": past_steps,
            "snapshot": snapshot, "tool_list": tool_list}


# --------------------------------------------------------------------------
# Grading one trace and writing the scores
# --------------------------------------------------------------------------

def score_trace(client, trace_id: str, run: dict) -> None:
    # New judges for every trace: a judge keeps the score of the last thing it graded
    judges = [
        ("plan_quality_sees_workspace", make_plan_judge(),
         plan_test_case(run["task"], run["plan"], run["snapshot"], run["tool_list"])),
        ("plan_adherence_custom", make_adherence_judge(),
         adherence_test_case(run["task"], run["plan"], run["past_steps"])),
    ]
    for name, judge, test_case in judges:
        try:
            judge.measure(test_case)
        except Exception:
            logger.exception("Could not grade %s for trace %s", name, trace_id)
            continue

        logger.info("%s %s = %.2f  %s", trace_id, name, judge.score, judge.reason)
        client.create_score(
            trace_id=trace_id,
            name=name,
            value=judge.score,
            data_type="NUMERIC",
            comment=judge.reason,
            score_id=f"{trace_id}_{name}",  # same id on a second run: the score is replaced, not added
            environment=ENVIRONMENT,  # a score does not take its trace's environment by itself
        )


def main() -> None:
    run_time = datetime.now(timezone.utc)
    client = get_client()
    # If this call fails, the script stops here and the checkpoint is not moved,
    # so the next run tries the same traces again
    trace_ids = fetch_new_trace_ids(client, since=load_checkpoint(), until=run_time)

    scored = skipped = failed = 0
    for trace_id in trace_ids:
        try:
            run = extract_run(fetch_observations(client, trace_id))
            if isinstance(run, str):
                logger.info("Skipped %s: %s", trace_id, run)
                skipped += 1
                continue
            score_trace(client, trace_id, run)
            scored += 1
        except Exception:  # one broken trace must not stop the others
            logger.exception("Could not grade trace %s", trace_id)
            failed += 1

    client.flush()  # send the scores before the script ends
    save_checkpoint(run_time)
    logger.info("Run complete: fetched=%d scored=%d skipped=%d failed=%d",
                len(trace_ids), scored, skipped, failed)


if __name__ == "__main__":
    main()
