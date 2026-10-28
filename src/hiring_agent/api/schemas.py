"""Request and response models for the FastAPI layer (Stage 6).

FastAPI uses them to validate what comes in, to shape what goes out, and to draw the
/docs page. They describe the HTTP contract only; the agent and workspace code don't
import them.
"""

from typing import Literal

from pydantic import BaseModel, Field, computed_field


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------

class TaskRequest(BaseModel):
    task: str = Field(min_length=1, description="One plain-English hiring task")


class Changes(BaseModel):
    """What one agent run did to the workspace (before/after snapshot diff)."""
    added: list[str]
    moved: list[tuple[str, str]]  # (old path, new path)
    deleted: list[str]
    new_rows: int                 # tracker rows added during the run


class AgentRunResponse(BaseModel):
    """The public part of the agent's final state, plus the workspace changes.

    guardrail_message (the real reason a guard blocked), tracker_before and score_checked
    are left out on purpose: the block reason stays in the server log so an attacker
    can't learn which check fired, and the other two are internal bookkeeping.
    """
    task: str
    plan: list[str]
    past_steps: list[tuple[str, str]]  # (step, result)
    response: str
    # None when no guard ran its check far enough to set them
    guardrail_status: str | None = None
    guardrail_stage: str | None = None
    changes: Changes


# --------------------------------------------------------------------------
# Workspace
# --------------------------------------------------------------------------

class WorkspaceResponse(BaseModel):
    tree: dict[str, list[str]]       # folder -> file paths
    tracker: list[dict[str, str]]    # candidates.csv rows
    has_job_description: bool
    upload_types: list[str]          # file extensions the upload accepts


# named FileContentResponse, not FileResponse, so it can't be confused with fastapi.responses.FileResponse
class FileContentResponse(BaseModel):
    path: str
    content: str


class UploadResponse(BaseModel):
    saved: list[str]    # workspace paths of the stored files
    errors: list[str]   # "<filename>: <reason>" for each file that was refused


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------

class HealthResponse(BaseModel):
    openai: Literal["healthy", "unhealthy"]
    openai_error: str | None = None
    jev: Literal["healthy", "unhealthy"]
    jev_error: str | None = None

    # the agent needs both services, so one down means the whole app is down
    @computed_field
    @property
    def status(self) -> Literal["healthy", "unhealthy"]:
        if self.openai == "healthy" and self.jev == "healthy":
            return "healthy"
        return "unhealthy"
