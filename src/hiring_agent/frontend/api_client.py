"""HTTP calls from the Streamlit app to the FastAPI backend (Stage 6).

One small function per endpoint. Each raises a requests.RequestException when the
backend can't be reached or answers with an error, so the app decides what to show.
"""

import os

import requests

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")

# a full screening run takes a minute or two, so the agent call gets a lot of room
AGENT_TIMEOUT = 600
HEALTH_TIMEOUT = 60
TIMEOUT = 15


def run_task(task: str) -> dict:
    """Run the agent on one task: the public final state plus the workspace changes."""
    response = requests.post(f"{API_BASE_URL}/agent/run", json={"task": task}, timeout=AGENT_TIMEOUT)
    response.raise_for_status()
    return response.json()


def get_workspace() -> dict:
    """File tree, tracker rows, whether a JD exists, and the accepted upload types."""
    response = requests.get(f"{API_BASE_URL}/workspace", timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def read_file(path: str) -> str:
    response = requests.get(f"{API_BASE_URL}/workspace/file", params={"path": path}, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()["content"]


def upload_files(job_description: tuple[str, bytes] | None, resumes: list[tuple[str, bytes]]) -> dict:
    """Send (filename, bytes) pairs as one multipart request; returns {saved, errors}."""
    files = [("resumes", resume) for resume in resumes]
    if job_description is not None:
        files.append(("job_description", job_description))
    response = requests.post(f"{API_BASE_URL}/workspace/upload", files=files, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def clear_workspace() -> dict:
    response = requests.post(f"{API_BASE_URL}/workspace/clear", timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def check_health() -> dict:
    """{openai, openai_error, jev, jev_error, status}"""
    response = requests.get(f"{API_BASE_URL}/health", timeout=HEALTH_TIMEOUT)
    response.raise_for_status()
    return response.json()
