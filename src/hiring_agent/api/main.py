"""FastAPI app around the hiring agent (Stage 6).

Run from the repo root:
    uv run uvicorn hiring_agent.api.main:app --reload
Then open http://127.0.0.1:8000/docs to try every endpoint.
"""

from fastapi import FastAPI

from hiring_agent.app import workspace

from hiring_agent.api.routers import agent, health
from hiring_agent.api.routers import workspace as workspace_router

# The API serves the Streamlit app, so every workspace call and every agent tool in this
# process uses workspace_app/ (filled by uploads). The seeded workspace/ is left to the
# CLI and the evals, which run in their own processes.
workspace.use_workspace(workspace.APP_WORKSPACE_DIR)
if not workspace.APP_WORKSPACE_DIR.exists():
    workspace.create_empty_workspace()

app = FastAPI(
    title="Hiring Assistant API",
    description="Runs the hiring agent and serves its workspace to the Streamlit frontend.",
)

app.include_router(agent.router)
app.include_router(workspace_router.router)
app.include_router(health.router)
