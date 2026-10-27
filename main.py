"""CLI runner: reset the workspace, run ONE task, print what the agent did.

    uv run python main.py "Screen Ananya Rao's resume and add her to the tracker"

With no argument it runs that same example task.

To test the prompt-injection guard, start with a flag that plants injected files:

    uv run python main.py --inject-resumes "Screen all resumes and add them to the tracker"
    uv run python main.py --inject-jd "Screen Ananya Rao's resume and add her to the tracker"
"""

import sys

from hiring_agent.app.agent import build_agent, run_agent
from hiring_agent.app.workspace import (
    ADVERSARIAL_JD,
    ADVERSARIAL_RESUMES,
    reset_workspace,
    workspace_snapshot,
)

DEFAULT_TASK = "Screen Ananya Rao's resume and add her to the tracker"

INJECT_FLAGS = {"--inject-resumes": ADVERSARIAL_RESUMES, "--inject-jd": ADVERSARIAL_JD}


def main() -> None:
    args = sys.argv[1:]
    extra_files = INJECT_FLAGS[args.pop(0)] if args and args[0] in INJECT_FLAGS else None
    task = " ".join(args) or DEFAULT_TASK

    # every run starts from the same seeded workspace (plus injected files if a flag was given)
    reset_workspace(extra_files=extra_files)
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
