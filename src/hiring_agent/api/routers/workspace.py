"""Workspace endpoints: what the frontend shows and the files the user uploads.

Each endpoint is a thin wrapper over hiring_agent/app/workspace.py, the same service
layer the agent's tools use. Its functions are called as `workspace.<name>` (never
imported by name) because they read the module-level WORKSPACE_DIR, which main.py
points at workspace_app/.
"""

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from hiring_agent.app import workspace

from hiring_agent.api.schemas import FileContentResponse, UploadResponse, WorkspaceResponse

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("", response_model=WorkspaceResponse)
def get_workspace() -> WorkspaceResponse:
    """File tree and tracker rows: everything the frontend draws on every rerun."""
    return WorkspaceResponse(
        tree=workspace.file_tree(),
        tracker=workspace.read_tracker(),
        has_job_description=(workspace.WORKSPACE_DIR / workspace.JD_FILE).is_file(),
        upload_types=workspace.UPLOAD_TYPES,
    )


@router.get("/file", response_model=FileContentResponse)
def read_file(path: str) -> FileContentResponse:
    """Text of one file, e.g. ?path=resumes/ananya_rao.txt"""
    try:
        content = workspace.read_text(path)
    except ValueError as error:  # not found, or outside the workspace (safe_path)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return FileContentResponse(path=path, content=content)


@router.post("/upload", response_model=UploadResponse)
def upload_files(
    job_description: Annotated[UploadFile | None, File()] = None,
    resumes: Annotated[list[UploadFile], File()] = [],
) -> UploadResponse:
    """Store an uploaded JD and/or resumes. A refused file is reported, not raised,
    so one bad file doesn't stop the others from being saved."""
    saved, errors = [], []
    if job_description is not None:
        try:
            saved.append(workspace.save_job_description(job_description.filename, job_description.file.read()))
        except ValueError as error:
            errors.append(f"{job_description.filename}: {error}")
    for resume in resumes:
        try:
            saved.append(workspace.save_resume(resume.filename, resume.file.read()))
        except ValueError as error:
            errors.append(f"{resume.filename}: {error}")
    return UploadResponse(saved=saved, errors=errors)


@router.post("/clear", response_model=WorkspaceResponse)
def clear_workspace() -> WorkspaceResponse:
    """Delete the JD, every resume, draft and tracker row; return the empty workspace."""
    workspace.create_empty_workspace()
    return get_workspace()
