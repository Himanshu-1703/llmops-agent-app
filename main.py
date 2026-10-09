"""CLI runner: reset the workspace, run ONE task, print what the agent did.

    uv run python main.py "Screen Ananya Rao's resume and add her to the tracker"

With no argument it runs that same example task.
"""

import sys

from hiring_agent.app.agent import build_agent, run_agent
from hiring_agent.app.workspace import reset_workspace, workspace_snapshot

DEFAULT_TASK = "Screen Ananya Rao's resume and add her to the tracker"


def main() -> None:
    task = " ".join(sys.argv[1:]) or DEFAULT_TASK

    # every run starts from the same seeded workspace
    reset_workspace()
    print(f"TASK: {task}\n")

    agent = build_agent()
    state = run_agent(agent, task, full=True)

    print("PLAN")
    for i, step in enumerate(state["plan"], start=1):
        print(f"  {i}. {step}")

    print("\nSTEP RESULTS")
    for i, (step, result) in enumerate(state["past_steps"], start=1):
        print(f"  {i}. {step}\n     -> {result}")

    print(f"\nRESPONSE\n  {state['response']}")
    print(f"\nWORKSPACE AFTER THE RUN\n{workspace_snapshot()}")


if __name__ == "__main__":
    main()
