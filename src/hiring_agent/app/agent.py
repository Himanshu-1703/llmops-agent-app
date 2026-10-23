"""Plan-and-execute hiring agent.

    START -> planner -> executor --(steps left?)--> executor ...
                                \\--(all done)-----> responder -> END

planner:   one LLM call that writes the plan (it sees the task, the tool list and a
           workspace snapshot of file paths + tracker rows, never file contents)
executor:  runs ONE plan step per visit with a small tool-calling sub-agent
responder: writes the final answer from the step results

The three system prompts are not in this file: they are loaded from Langfuse
(label "latest") by prompts.load_prompt().
"""

import operator
from typing import Annotated, TypedDict

from langchain.agents import create_agent
from langchain_core.prompts import PromptTemplate
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from hiring_agent.app.clients import agent_params
from hiring_agent.app.clients import llm as default_llm
from hiring_agent.app.clients import logger
from hiring_agent.app.clients import planner_llm as default_planner_llm
from hiring_agent.app.clients import responder_llm as default_responder_llm
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
    # a full screening run is ~30 executor visits, so params.yaml gives the graph plenty of room
    final_state = agent.invoke(
        {"task": task, "past_steps": []},
        config={"recursion_limit": agent_params.recursion_limit, "callbacks": callbacks or []},
    )
    return final_state if full else final_state["response"]
