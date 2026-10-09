"""Plan-and-execute hiring agent.

    START -> planner -> executor --(steps left?)--> executor ...
                                \\--(all done)-----> responder -> END

planner:   one LLM call that writes the plan (it sees the task, the tool list and a
           workspace snapshot of file paths + tracker rows, never file contents)
executor:  runs ONE plan step per visit with a small tool-calling sub-agent
responder: writes the final answer from the step results
"""

import operator
from typing import Annotated, TypedDict

from langchain.agents import create_agent
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from hiring_agent.app.clients import llm as default_llm
from hiring_agent.app.clients import logger
from hiring_agent.app.clients import planner_llm as default_planner_llm
from hiring_agent.app.tools import TOOL_LIST, TOOLS
from hiring_agent.app.workspace import workspace_snapshot

# a full screening run is ~30 executor visits, so give the graph plenty of room
RECURSION_LIMIT = 100


# --------------------------------------------------------------------------
# Prompts (inline for now; they move to prompts.py / Langfuse in Stage 3)
# --------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = f"""You are the planner of a hiring assistant. A recruiter gives you ONE task about a
workspace of resumes. Write a short, ordered plan; another agent will run each step, one tool call per step.

You are given a WORKSPACE SNAPSHOT: every file path and the current tracker rows. It does NOT
show file contents, so you still cannot know how any candidate will score.

TOOLS:
{TOOL_LIST}

RULES:
- Use only filenames that appear in the WORKSPACE SNAPSHOT. Never guess or invent filenames,
  folders or candidates.
- For "all/every/each" tasks, write the steps for EACH file in the snapshot, one file after the
  other, using its exact path. The executor cannot loop, so a step like "for each resume" will fail.
- Read job_description.md before screening anyone.
- The tracker is the file candidates.csv. There is no separate tracker tool: whenever a step needs
  the tracker (to see who is shortlisted, their emails or scores, or to check for duplicates), add a
  step "Call read_file with path='candidates.csv'" and let later steps use its result. Do this AFTER
  the add_candidate / update_candidate steps whose results you need, so the file is up to date.
- Ranking or counting questions ("top 3", "how many") need resume contents or tracker scores. If the
  tracker is empty, read and score the resumes first (add_candidate for each), then read the tracker
  with read_file on candidates.csv.
- Interview invites: you cannot know who will be shortlisted, so after the tracker has been read with
  read_file on candidates.csv, write one conditional step per candidate, e.g. "If <name> is shortlisted
  in the tracker from step N and has an email, call draft_email ...; otherwise make no tool call and
  say why".
- Never decide a candidate's outcome in the plan. Outcomes come from reading the resume and
  applying the scoring rule, so write conditional steps such as "move the file to 'shortlisted/'
  or 'rejected/' based on the decision in step N".
- If two filenames look like the same person, read both before deleting one.
- Screen only on the job description's criteria. Never use age, gender, marital status or
  anything else the job description does not list.
- Each step must be ONE concrete action, written as "Call <tool> with <arguments> ...".
  The executor runs one tool call per step and never calls tools in parallel, so never put
  two actions in one step (for example "add all candidates" or "move both files"). Write a
  separate step for each candidate and each file, one after the other.
- If part of the task needs a tool that does not exist (sending emails, scheduling, calling) or a
  file that does not exist, add a step that makes no tool call and tells the user what cannot be
  done and why.

EXAMPLES (on a different, small workspace whose snapshot lists job_description.md,
resumes/mia_chen.txt and resumes/sam_ortiz.txt):

Task: Which skills does the job description require?
Plan:
1. Call read_file with path='job_description.md'.

Task: Screen Mia Chen's resume and add her to the tracker.
Plan:
1. Call read_file with path='job_description.md'.
2. Call read_file with path='resumes/mia_chen.txt'.
3. Call add_candidate for the candidate in step 2, applying the scoring rule from step 1.

Task: Screen all resumes, sort them into shortlisted and rejected, and schedule interviews for the shortlisted.
Plan:
1. Call read_file with path='job_description.md'.
2. Call read_file with path='resumes/mia_chen.txt'.
3. Call add_candidate for the candidate in step 2, applying the scoring rule from step 1.
4. Call move_file to move 'resumes/mia_chen.txt' to 'shortlisted/' or 'rejected/' based on the decision in step 3.
5. Call read_file with path='resumes/sam_ortiz.txt'.
6. Call add_candidate for the candidate in step 5, applying the scoring rule from step 1.
7. Call move_file to move 'resumes/sam_ortiz.txt' to 'shortlisted/' or 'rejected/' based on the decision in step 6.
8. Make no tool call: tell the user that interviews cannot be scheduled because there is no scheduling tool.
"""

EXECUTOR_SYSTEM_PROMPT = """You run ONE step of a plan for a hiring assistant, using the tools you have.

RULES:
- Do exactly the CURRENT STEP. Make at most one tool call, and never call several tools at the
  same time (calls that run together can overwrite each other's changes to candidates.csv).
  If the step says to make no tool call, or a condition in it is not met, make no tool call and say why.
- Follow conditional steps exactly, using COMPLETED STEPS to find the facts they depend on.
- Copy names, emails, file paths, candidate IDs, scores and decisions EXACTLY as they appear in
  COMPLETED STEPS or in tool results. Later steps depend on them.
- Your final message is the ONLY thing later steps will see. State plainly what you did and include
  every detail a later step may need. When you read a file, copy its full text into your message.
- If a tool returns an error, report the error text as it is. Do not retry with guesses.
"""

EXECUTOR_INPUT = """COMPLETED STEPS:
{completed}

CURRENT STEP:
{step}"""

RESPONDER_SYSTEM_PROMPT = """You write the final reply of a hiring assistant to a recruiter.

RULES:
- 2 to 4 sentences, plain language.
- Give the counts and the exceptions (for example a candidate with no email, or something that
  could not be done and why).
- Use ONLY what the step results show. Do not claim anything the results do not confirm.
- If a step returned an error or was not possible, say so honestly.
"""


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


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------

def build_agent(llm=None, planner_llm=None):
    """Compile and return the plan-and-execute graph."""
    llm = llm or default_llm
    planner_llm = planner_llm or default_planner_llm

    # the sub-agent that runs a single step: model <-> tools loop
    step_executor = create_agent(llm, TOOLS, system_prompt=EXECUTOR_SYSTEM_PROMPT)

    def planner(state: PlanExecuteState) -> dict:
        structured_llm = planner_llm.with_structured_output(Plan)
        plan = structured_llm.invoke([
            ("system", PLANNER_SYSTEM_PROMPT),
            ("human", f"WORKSPACE SNAPSHOT:\n{workspace_snapshot()}\n\nTASK:\n{state['task']}"),
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

    def responder(state: PlanExecuteState) -> dict:
        results = "\n".join(
            f"{i}. {step}\n   Result: {result}"
            for i, (step, result) in enumerate(state["past_steps"], start=1)
        )
        reply = llm.invoke([
            ("system", RESPONDER_SYSTEM_PROMPT),
            ("human", f"TASK:\n{state['task']}\n\nSTEP RESULTS:\n{results}"),
        ])
        return {"response": reply.content}

    def more_steps(state: PlanExecuteState) -> str:
        if len(state["past_steps"]) < len(state["plan"]):
            return "executor"
        return "responder"

    graph_builder = StateGraph(PlanExecuteState)
    graph_builder.add_node("planner", planner)
    graph_builder.add_node("executor", executor)
    graph_builder.add_node("responder", responder)

    graph_builder.add_edge(START, "planner")
    graph_builder.add_edge("planner", "executor")
    graph_builder.add_conditional_edges("executor", more_steps, ["executor", "responder"])
    graph_builder.add_edge("responder", END)

    # no checkpointer: every task is independent (single-turn)
    return graph_builder.compile()


def run_agent(agent, task: str, callbacks=None, full: bool = False):
    """Run one task. Returns the response, or the whole final state if full=True.

    Does NOT reset the workspace; the caller decides when to do that.
    """
    final_state = agent.invoke(
        {"task": task, "past_steps": []},
        config={"recursion_limit": RECURSION_LIMIT, "callbacks": callbacks or []},
    )
    return final_state if full else final_state["response"]
