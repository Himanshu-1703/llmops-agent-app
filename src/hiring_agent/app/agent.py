"""Plan-and-execute hiring agent.

    START -> input_guard --(ok)--> planner -> action_guard --(ok)--> executor --(steps left?)--> executor ...
                  |                                |                    ^  \\--(all done)--> score_check
                  \\--(exception)--> guard_fallback <--(exception)--/    |                      |
                                           \\--> END                     \\--(correction steps)--+
                                                                                              \\--(none)--> responder -> END

input_guard:    checks the task (jailbreak, off-topic, discriminatory) and the JD (prompt
                injection) before anything runs; resumes are checked inside read_file
action_guard:   checks the plan before its first step runs (too many certain deletes,
                protected files); delete_file also caps deletes while the run goes
guard_fallback: replies with one generic message when a guard blocks
planner:        one LLM call that writes the plan (it sees the task, the tool list and a
                workspace snapshot of file paths + tracker rows, never file contents)
executor:       runs ONE plan step per visit with a small tool-calling sub-agent
score_check:    once, after the last step: verifies the fit_score of each new tracker row
                (LLM + calculator) and adds a correction step for each wrong one
responder:      writes the final answer from the step results

The three system prompts are not in this file: they are loaded from Langfuse
(label "latest") by prompts.load_prompt().
"""

import operator
from typing import Annotated, TypedDict

from langchain.agents import create_agent
from langchain_core.prompts import PromptTemplate
from guardrails.errors import ValidationError
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typesafe_sdk import TypeSafeError

from hiring_agent.app.clients import agent_params
from hiring_agent.app.clients import llm as default_llm
from hiring_agent.app.clients import logger
from hiring_agent.app.clients import planner_llm as default_planner_llm
from hiring_agent.app.clients import responder_llm as default_responder_llm
from hiring_agent.app import workspace
from hiring_agent.app.guards import (
    GENERIC_BLOCK_MESSAGE,
    JD_PATH,
    delete_limit,
    validate_document,
    validate_input,
    validate_plan,
    validate_score,
)
from hiring_agent.app.prompts import (
    EXECUTOR_PROMPT_NAME,
    PLANNER_PROMPT_NAME,
    RESPONDER_PROMPT_NAME,
    load_prompt,
)
from hiring_agent.app.tools import TOOL_LIST, TOOLS
from hiring_agent.app.workspace import workspace_snapshot

# The message each node sends with its run-time data. These are not versioned in Langfuse:
# they only carry values, the behaviour comes from the system prompts.
PLANNER_INPUT = PromptTemplate.from_template("WORKSPACE SNAPSHOT:\n{snapshot}\n\nTASK:\n{task}")
EXECUTOR_INPUT = PromptTemplate.from_template("COMPLETED STEPS:\n{completed}\n\nCURRENT STEP:\n{step}")
RESPONDER_INPUT = PromptTemplate.from_template("TASK:\n{task}\n\nSTEP RESULTS:\n{results}")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class Plan(BaseModel):
    steps: list[str] = Field(description="Ordered, concrete steps to complete the task")


class PlanExecuteState(TypedDict):
    task: str
    plan: list[str]
    # (step, result) pairs; operator.add appends. Safe because every task is a fresh invoke.
    past_steps: Annotated[list[tuple[str, str]], operator.add]
    response: str
    # set by the guard nodes (same names as the RAG app)
    guardrail_status: str   # "ok" or "exception"
    guardrail_stage: str    # which guard decided, e.g. "input"
    guardrail_message: str  # the real reason, for the log only; never shown to the user
    tracker_before: list[str]  # candidate_ids at the start of the run (score_check checks only new rows)
    score_checked: bool        # True once score_check has run, so its correction loop runs only once


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------

def build_agent(llm=None, planner_llm=None, responder_llm=None):
    """Compile and return the plan-and-execute graph.

    The prompts are fetched from Langfuse here, so a new "latest" version is picked up
    the next time an agent is built (Langfuse's SDK also caches prompts for ~60 seconds).
    """
    llm = llm or default_llm
    planner_llm = planner_llm or default_planner_llm
    responder_llm = responder_llm or default_responder_llm

    # system prompts from Langfuse; only the planner's has a variable (the tool list)
    planner_system = load_prompt(PLANNER_PROMPT_NAME).compile(tool_list=TOOL_LIST)
    executor_system = load_prompt(EXECUTOR_PROMPT_NAME).compile()
    responder_system = load_prompt(RESPONDER_PROMPT_NAME).compile()

    # the sub-agent that runs a single step: model <-> tools loop
    step_executor = create_agent(llm, TOOLS, system_prompt=executor_system)

    def input_guard(state: PlanExecuteState) -> dict:
        # first node of every run: start this run's delete count from zero
        delete_limit.reset()
        tracker_before = [row["candidate_id"] for row in workspace.read_tracker()]
        try:
            validate_input(state["task"])
            # every task depends on the JD, so a poisoned JD stops the whole run here
            try:
                jd_text = workspace.read_text(JD_PATH)
            except ValueError:
                jd_text = None  # no JD: nothing to check, the agent will report it missing
            if jd_text is not None:
                validate_document(jd_text, path=JD_PATH)
        except ValidationError as error:
            # a validator with on_fail="exception" failed
            logger.warning("Input guard blocked the run: %s", error)
            return {"guardrail_status": "exception", "guardrail_stage": "input", "guardrail_message": str(error)}
        except TypeSafeError as error:
            # Jev could not be reached: block rather than let an unchecked task through
            logger.error("Input guard could not run (Jev call failed): %s", error)
            return {"guardrail_status": "exception", "guardrail_stage": "input", "guardrail_message": f"Jev call failed: {error}"}
        return {
            "guardrail_status": "ok", "guardrail_stage": "input", "guardrail_message": "",
            "tracker_before": tracker_before, "score_checked": False,
        }

    def action_guard(state: PlanExecuteState) -> dict:
        # checked before the first step runs, so a blocked plan changes nothing
        try:
            if state["plan"]:
                validate_plan(state["plan"])
        except ValidationError as error:
            logger.warning("Action guard blocked the plan: %s", error)
            return {"guardrail_status": "exception", "guardrail_stage": "action", "guardrail_message": str(error)}
        except TypeSafeError as error:
            logger.error("Action guard could not run (Jev call failed): %s", error)
            return {"guardrail_status": "exception", "guardrail_stage": "action", "guardrail_message": f"Jev call failed: {error}"}
        return {"guardrail_status": "ok", "guardrail_stage": "action", "guardrail_message": ""}

    def guard_fallback(state: PlanExecuteState) -> dict:
        # the same message whatever guard fired; the reason is already in the log
        return {"plan": [], "response": GENERIC_BLOCK_MESSAGE}

    def guardrail_router(state: PlanExecuteState) -> str:
        return state["guardrail_status"]

    def planner(state: PlanExecuteState) -> dict:
        structured_llm = planner_llm.with_structured_output(Plan)
        plan = structured_llm.invoke([
            ("system", planner_system),
            ("human", PLANNER_INPUT.format(snapshot=workspace_snapshot(), task=state["task"])),
        ])
        logger.info("Plan has %d steps", len(plan.steps))
        return {"plan": plan.steps}

    def executor(state: PlanExecuteState) -> dict:
        # steps are run in order, so the number done tells us which one is next
        step = state["plan"][len(state["past_steps"])]

        completed = "\n".join(
            f"{i}. {done_step}\n   Result: {result}"
            for i, (done_step, result) in enumerate(state["past_steps"], start=1)
        ) or "(none yet)"

        # the executor never sees the task or the rest of the plan
        result = step_executor.invoke({
            "messages": [("human", EXECUTOR_INPUT.format(completed=completed, step=step))]
        })
        # keep only the sub-agent's final text
        return {"past_steps": [(step, result["messages"][-1].content)]}

    def score_check(state: PlanExecuteState) -> dict:
        """Verify the fit_score of every tracker row this run added.

        A wrong score is not blocked: the node appends a correction step to the plan, and
        the executor recalculates and fixes the row with update_candidate.
        """
        job_description = workspace.read_text(JD_PATH)
        new_rows = [row for row in workspace.read_tracker() if row["candidate_id"] not in state["tracker_before"]]

        corrections = []
        for row in new_rows:
            # the resume may have been moved to shortlisted/ or rejected/ since it was added
            resume_path = workspace.find_file(row["resume_file"].split("/")[-1])
            if resume_path is None:
                logger.warning("Score check skipped %s: resume '%s' not found", row["candidate_id"], row["resume_file"])
                continue

            resume = workspace.read_text(resume_path)
            # Re-score only if the resume is this candidate's. A row can point at someone
            # else's file (the executor once added "Priya Nair" with meera_joshi.txt); scoring
            # that file would "correct" a right score into a wrong one.
            if row["name"].strip().lower() not in resume.lower():
                logger.warning(
                    "Score check refused %s (%s): '%s' is not this candidate's resume",
                    row["candidate_id"], row["name"], resume_path,
                )
                corrections.append(
                    f"Make no tool call: tell the recruiter that tracker row {row['candidate_id']} "
                    f"({row['name']}) points to '{row['resume_file']}', which is not {row['name']}'s "
                    f"resume, so this row's score was not checked and must be reviewed by hand."
                )
                continue

            outcome = validate_score(row, resume=resume, job_description=job_description)
            if not outcome.validation_passed:  # on_fail="refrain"
                reason = outcome.validation_summaries[0].failure_reason
                logger.warning("Score check: %s", reason)
                corrections.append(
                    f"{reason} Recalculate this candidate's ATS score from the resume and the job "
                    f"description, then call update_candidate for {row['candidate_id']} with field "
                    f"'fit_score' and the recalculated score."
                )

        logger.info("Score check: %d new rows checked, %d corrections", len(new_rows), len(corrections))
        return {"plan": state["plan"] + corrections, "score_checked": True}

    def responder(state: PlanExecuteState) -> dict:
        results = "\n".join(
            f"{i}. {step}\n   Result: {result}"
            for i, (step, result) in enumerate(state["past_steps"], start=1)
        )
        reply = responder_llm.invoke([
            ("system", responder_system),
            ("human", RESPONDER_INPUT.format(task=state["task"], results=results)),
        ])
        return {"response": reply.content}

    def more_steps(state: PlanExecuteState) -> str:
        if len(state["past_steps"]) < len(state["plan"]):
            return "executor"
        # all steps done: check the scores once, before the final answer
        if not state["score_checked"]:
            return "score_check"
        return "responder"

    graph_builder = StateGraph(PlanExecuteState)
    graph_builder.add_node("input_guard", input_guard)
    graph_builder.add_node("guard_fallback", guard_fallback)
    graph_builder.add_node("planner", planner)
    graph_builder.add_node("action_guard", action_guard)
    graph_builder.add_node("executor", executor)
    graph_builder.add_node("score_check", score_check)
    graph_builder.add_node("responder", responder)

    graph_builder.add_edge(START, "input_guard")
    graph_builder.add_conditional_edges(
        "input_guard", guardrail_router, {"ok": "planner", "exception": "guard_fallback"}
    )
    graph_builder.add_edge("guard_fallback", END)
    graph_builder.add_edge("planner", "action_guard")
    graph_builder.add_conditional_edges(
        "action_guard", guardrail_router, {"ok": "executor", "exception": "guard_fallback"}
    )
    graph_builder.add_conditional_edges("executor", more_steps, ["executor", "score_check", "responder"])
    # after the check: run any correction steps it added, otherwise answer
    graph_builder.add_conditional_edges("score_check", more_steps, ["executor", "responder"])
    graph_builder.add_edge("responder", END)

    # no checkpointer: every task is independent (single-turn)
    return graph_builder.compile()


def run_agent(agent, task: str, callbacks=None, full: bool = False):
    """Run one task. Returns the response, or the whole final state if full=True.

    Does NOT reset the workspace; the caller decides when to do that.
    """
    # a full screening run is ~30 executor visits, so params.yaml gives the graph plenty of room
    final_state = agent.invoke(
        {"task": task, "past_steps": []},
        config={"recursion_limit": agent_params.recursion_limit, "callbacks": callbacks or []},
    )
    return final_state if full else final_state["response"]
