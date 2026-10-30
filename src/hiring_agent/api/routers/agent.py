"""POST /agent/run: run the agent on one task and return its final state.

The same build_agent / run_agent the CLI and the evals use. The before/after workspace
diff that the Stage 1 frontend computed now happens here, so the frontend only shows it.
"""

from fastapi import APIRouter, HTTPException, status
from langfuse import get_client, propagate_attributes
from langfuse.langchain import CallbackHandler

from hiring_agent.app import workspace
from hiring_agent.app.agent import build_agent, run_agent
from hiring_agent.app.clients import logger

from hiring_agent.api.schemas import AgentRunResponse, Changes, TaskRequest

router = APIRouter(prefix="/agent", tags=["agent"])

# compiled once when the server starts, reused for every request (single-turn: no
# checkpointer, so nothing is carried from one task to the next)
agent = build_agent()

# Created after the hiring_agent.app imports, so clients.py has already loaded .env and
# LANGFUSE_TRACING_ENABLED is set before the first get_client(). With the flag off
# (CLI, evals, CI) the span and the handler record nothing.
langfuse = get_client()
langfuse_handler = CallbackHandler()

TRACE_NAME = "hiring-agent-run"


# --------------------------------------------------------------------------
# What changed in the workspace during a run (moved here from the Stage 1 frontend)
# --------------------------------------------------------------------------

def take_snapshot() -> dict:
    """Every file path and every tracker id, taken before and after a run."""
    files = {path for paths in workspace.file_tree().values() for path in paths}
    tracker_ids = {row["candidate_id"] for row in workspace.read_tracker()}
    return {"files": files, "tracker_ids": tracker_ids}


def diff_snapshots(before: dict, after: dict) -> Changes:
    """Added, moved and deleted files plus new tracker rows."""
    added = sorted(after["files"] - before["files"])
    deleted = sorted(before["files"] - after["files"])

    # a file that left one folder and appeared in another with the same name was moved
    moved = []
    for old in list(deleted):
        name = old.split("/")[-1]
        new = next((path for path in added if path.split("/")[-1] == name), None)
        if new:
            moved.append((old, new))
            deleted.remove(old)
            added.remove(new)

    new_rows = len(after["tracker_ids"] - before["tracker_ids"])
    return Changes(added=added, moved=moved, deleted=deleted, new_rows=new_rows)


@router.post("/run", response_model=AgentRunResponse)
def run(request: TaskRequest) -> AgentRunResponse:
    # A plain `def`, so FastAPI runs it in a worker thread: a 1-2 minute run does not
    # block other requests such as /health or /workspace.
    before = take_snapshot()

    # One trace per task. The root span sets the trace's input (the task) and output (the
    # response) explicitly, instead of letting the raw graph state become the trace I/O.
    # The CallbackHandler nests every graph node, LLM call and tool call under it.
    with langfuse.start_as_current_observation(
        as_type="span",
        name=TRACE_NAME,
        input=request.task,
    ) as root_span:
        with propagate_attributes(trace_name=TRACE_NAME):
            try:
                state = run_agent(agent, request.task, callbacks=[langfuse_handler], full=True)
            except Exception as error:
                logger.error(f"[agent] run failed: {type(error).__name__}: {error}")
                root_span.update(level="ERROR", status_message=str(error))
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to run the agent.",
                ) from error
        root_span.update(output=state["response"])

    # AgentRunResponse declares only the public keys, so guardrail_message (already in
    # the server log), tracker_before and score_checked are dropped here
    return AgentRunResponse(**state, changes=diff_snapshots(before, take_snapshot()))
