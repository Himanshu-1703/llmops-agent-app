"""Streamlit frontend: the workspace view plus the agent panel.

Stage 6: the app no longer imports the agent. Every workspace read, upload, clear and
agent run is an HTTP request to the FastAPI backend (api_client.py). The backend works
on workspace_app/; the seeded workspace/ is left to the CLI and the evals.

Run from the repo root, in two terminals:
    uv run uvicorn hiring_agent.api.main:app --reload
    uv run streamlit run src/hiring_agent/frontend/agent_app.py
"""

import pandas as pd
import requests
import streamlit as st

from hiring_agent.frontend import api_client

st.set_page_config(
    page_title="Hiring assistant",
    page_icon=":material/work:",
    layout="wide",
)

JD_FILE = "job_description.md"
BOX_HEIGHT = 560
DRAFTS_HEIGHT = 260

# Folders shown as KPI cards, in the order a run fills them.
KPI_FOLDERS = [
    ("resumes", "Resumes to screen", ":material/description:"),
    ("shortlisted", "Shortlisted", ":material/check_circle:"),
    ("rejected", "Rejected", ":material/cancel:"),
    ("drafts", "Email drafts", ":material/mail:"),
]

# Folders whose files can be previewed in the Resumes box, with a badge per folder.
RESUME_FOLDERS = {
    "resumes": "",
    "shortlisted": ":green-badge[shortlisted]",
    "rejected": ":red-badge[rejected]",
}


# Glow colours of the health button once it has been clicked (white until then)
HEALTH_COLOURS = {"healthy": "#21c354", "unhealthy": "#ff4b4b"}


# --------------------------------------------------------------------------
# API health (sidebar): one button for both OpenAI and Jev
# --------------------------------------------------------------------------

def render_dot(status: str | None) -> str:
    """Same dots as the RAG app: white before the first check, then green or red."""
    if status == "healthy":
        return "🟢"
    if status == "unhealthy":
        return "🔴"
    return "⚪"


def run_health_check() -> None:
    """Button callback: ask the backend, store the answer for this rerun to draw."""
    try:
        st.session_state.health = api_client.check_health()
    except requests.RequestException:
        # no answer at all: the backend itself is down, so the whole app is unhealthy
        st.session_state.health = {"status": "unhealthy", "backend_down": True}


def show_health_button() -> None:
    health = st.session_state.health
    status = health["status"] if health else None
    st.button(
        f"{render_dot(status)} API health",
        key="health_button",
        on_click=run_health_check,
        width="stretch",
        help="Checks that the OpenAI and Jev APIs are working.",
    )
    if status is None:
        return

    # the glow: Streamlit gives the button the CSS class st-key-<key>
    colour = HEALTH_COLOURS[status]
    st.html(
        f"<style>.st-key-health_button button {{"
        f"border-color: {colour}; box-shadow: 0 0 12px {colour};"
        f"}}</style>"
    )
    if health.get("backend_down"):
        st.caption(f"Backend not reachable at {api_client.API_BASE_URL}")
    elif status == "unhealthy":
        down = [name for name, key in (("OpenAI", "openai"), ("Jev", "jev")) if health[key] != "healthy"]
        st.caption(f"Unhealthy: {', '.join(down)}")


# --------------------------------------------------------------------------
# Workspace view (main area)
# --------------------------------------------------------------------------

def show_kpis(tree: dict[str, list[str]]) -> None:
    """One bordered card per folder, so a run's effect is visible at a glance."""
    for column, (folder, label, icon) in zip(st.columns(len(KPI_FOLDERS)), KPI_FOLDERS):
        with column.container(border=True):
            st.metric(f"{icon} {label}", len(tree.get(folder, [])))


def show_last_run_changes(changes: dict | None) -> None:
    """Summary of what the latest agent run did to the workspace."""
    if changes is None:
        return
    with st.container(border=True):
        st.markdown("**:material/history: Changes in the last run**")
        counts = {
            ":blue-badge[{} moved]": len(changes["moved"]),
            ":green-badge[{} added]": len(changes["added"]),
            ":red-badge[{} deleted]": len(changes["deleted"]),
            ":orange-badge[{} tracker rows added]": changes["new_rows"],
        }
        badges = [template.format(n) for template, n in counts.items() if n]
        if not badges:
            st.caption("The run did not change any file or the tracker.")
            return
        st.markdown(" ".join(badges))

        # tracker-only runs have no file lines, so there is nothing to expand
        if changes["moved"] or changes["added"] or changes["deleted"]:
            with st.expander("Details"):
                for old, new in changes["moved"]:
                    st.markdown(f":blue[:material/drive_file_move:] `{old}` → `{new}`")
                for path in changes["added"]:
                    st.markdown(f":green[:material/add_circle:] `{path}`")
                for path in changes["deleted"]:
                    st.markdown(f":red[:material/delete:] `{path}`")


def smaller_headings(markdown: str) -> str:
    """Push every heading two levels down ('#' -> '###') so the file fits in the box."""
    return "\n".join(
        "##" + line if line.startswith("#") else line for line in markdown.splitlines()
    )


def show_job_description(has_jd: bool) -> None:
    """Always visible: the screening criteria the agent works from."""
    with st.container(border=True):
        st.markdown("**:material/work: Job description**")
        with st.container(height=BOX_HEIGHT, border=False):
            if not has_jd:
                st.caption("No job description yet. Upload one in the sidebar.")
                return
            st.markdown(smaller_headings(api_client.read_file(JD_FILE)))


def resume_label(path: str, moved_to: set[str]) -> str:
    """File name plus a badge for the folder it sits in (and 'moved' after a run)."""
    folder, name = path.split("/")
    label = f"{name} {RESUME_FOLDERS[folder]}".strip()
    if path in moved_to:
        label += " :blue-badge[moved]"
    return label


def show_resumes(tree: dict[str, list[str]], changes: dict | None) -> None:
    """Radio list of every resume (wherever it is now), previewed beside the list."""
    resume_paths = [path for folder in RESUME_FOLDERS for path in tree.get(folder, [])]
    moved_to = {new for _, new in changes["moved"]} if changes else set()

    with st.container(border=True):
        st.markdown(f"**:material/description: Resumes** · {len(resume_paths)}")
        if not resume_paths:
            st.caption("No resumes yet. Upload them in the sidebar.")
            return

        pick, preview = st.columns([1, 2])
        with pick.container(height=BOX_HEIGHT, border=False):
            selected = st.radio(
                "Resumes",
                resume_paths,
                index=None,
                key="selected_resume",
                format_func=lambda path: resume_label(path, moved_to),
                label_visibility="collapsed",
            )
        with preview.container(height=BOX_HEIGHT, border=False):
            if selected is None:
                st.caption("Select a resume to preview it.")
            else:
                st.code(api_client.read_file(selected), language="text", wrap_lines=True)


def split_draft(text: str) -> tuple[str, str, str]:
    """Split a saved draft ('To: ..', 'Subject: ..', blank line, body) into its parts."""
    header, _, body = text.partition("\n\n")
    fields = dict(line.split(": ", 1) for line in header.splitlines() if ": " in line)
    return fields.get("To", ""), fields.get("Subject", ""), body.strip()


def show_drafts(draft_paths: list[str]) -> None:
    """Radio list of the email drafts in drafts/, with the selected one shown as an email."""
    with st.container(border=True):
        st.markdown(f"**:material/mail: Email drafts** · {len(draft_paths)}")
        if not draft_paths:
            st.caption("No drafts yet. The agent saves interview invites here; it never sends them.")
            return

        pick, preview = st.columns([1, 2])
        with pick.container(height=DRAFTS_HEIGHT, border=False):
            selected = st.radio(
                "Email drafts",
                draft_paths,
                index=None,
                key="selected_draft",
                # 'invite_rohan_mehta_example_com.txt' -> 'rohan_mehta_example_com'
                format_func=lambda path: path.split("/")[-1].removeprefix("invite_").removesuffix(".txt"),
                label_visibility="collapsed",
            )
        with preview.container(height=DRAFTS_HEIGHT, border=False):
            if selected is None:
                st.caption("Select a draft to read it.")
                return
            to, subject, body = split_draft(api_client.read_file(selected))
            st.markdown(f"**To:** {to}")
            st.markdown(f"**Subject:** {subject}")
            st.text(body)


def show_tracker(rows: list[dict]) -> None:
    with st.container(border=True):
        st.markdown(f"**:material/table_chart: Candidate tracker** · {len(rows)} rows")
        if not rows:
            st.caption("No candidates yet.")
            return
        st.dataframe(
            pd.DataFrame(rows),
            hide_index=True,
            alt="Candidate tracker with decision and fit score for each screened resume",
            # the outcome columns come first so they are visible without scrolling
            column_order=[
                "candidate_id", "name", "decision", "fit_score", "years_experience",
                "must_haves_met", "email", "nice_to_haves", "resume_file", "notes",
            ],
            column_config={
                "candidate_id": st.column_config.TextColumn("ID", pinned=True),
                "name": st.column_config.TextColumn("Name", pinned=True),
                "decision": "Decision",
                "fit_score": st.column_config.ProgressColumn(
                    "ATS score", min_value=0, max_value=100, format="%d"
                ),
                "years_experience": "Years",
                "must_haves_met": "Must-haves met",
                "email": "Email",
                "nice_to_haves": "Nice-to-haves",
                "resume_file": "Resume file",
                "notes": "Notes",
            },
        )


# --------------------------------------------------------------------------
# Uploads and agent panel (sidebar)
# --------------------------------------------------------------------------

def save_uploads(jd_file, resume_files: list) -> None:
    """Send the uploaded files to the backend; report what was saved and what wasn't."""
    result = api_client.upload_files(
        (jd_file.name, jd_file.getvalue()) if jd_file is not None else None,
        [(file.name, file.getvalue()) for file in resume_files],
    )
    if result["saved"]:
        st.toast(f"Added {len(result['saved'])} file(s) to the workspace.", icon=":material/upload_file:")
    for message in result["errors"]:
        st.warning(message, icon=":material/warning:")


def show_upload_form(upload_types: list[str]) -> bool:
    """One form for the JD and the resumes, so files are saved once, on submit.
    Returns True if files were sent, so the caller can read the workspace again."""
    with st.form("uploads", clear_on_submit=True, border=True):
        jd_file = st.file_uploader("Job description", type=upload_types)
        resume_files = st.file_uploader(
            "Resumes", type=upload_types, accept_multiple_files=True
        )
        submitted = st.form_submit_button(
            "Add to workspace", icon=":material/upload_file:", width="stretch"
        )
    if submitted:
        save_uploads(jd_file, resume_files or [])
    return submitted


def clear_everything() -> None:
    """Button callback: empty the workspace and forget the previous runs."""
    api_client.clear_workspace()
    st.session_state.history = []
    st.session_state.last_changes = None
    st.session_state.selected_resume = None
    st.session_state.selected_draft = None


def run_task(task: str) -> dict:
    """Send one task to the backend; it runs the agent and diffs the workspace."""
    try:
        result = api_client.run_task(task)
    except requests.RequestException as error:  # show the failure in the panel instead of crashing the page
        return {"task": task, "error": f"The agent run failed: {error}"}

    st.session_state.last_changes = result["changes"]
    return {
        "task": task,
        "response": result["response"],
        "plan": result["plan"],
        "past_steps": result["past_steps"],
        "changes": result["changes"],
    }


def show_answer(entry: dict) -> None:
    """The agent's reply, with the plan and every step result in an expander."""
    if "error" in entry:
        st.error(entry["error"], icon=":material/error:")
        return

    st.write(entry["response"])
    with st.expander("Plan and step results", icon=":material/list_alt:"):
        for i, (step, result) in enumerate(entry["past_steps"], start=1):
            st.markdown(f"**{i}. {step}**")
            st.code(result, language="text", wrap_lines=True)


def show_history(entries: list[dict]) -> None:
    for entry in entries:
        with st.chat_message("user"):
            st.write(entry["task"])
        with st.chat_message("assistant"):
            show_answer(entry)


# --------------------------------------------------------------------------
# Page
#
# Order matters: the workspace is drawn BEFORE the agent runs, so it is not greyed
# out as stale while the agent works. After a run we rerun the script once so the
# workspace is drawn again from the new files.
# --------------------------------------------------------------------------

if "history" not in st.session_state:
    st.session_state.history = []
    st.session_state.last_changes = None
    st.session_state.health = None

st.title("Hiring assistant", icon=":material/work:")

with st.sidebar:
    # drawn first, so it still works when the backend is down
    show_health_button()

# one request per rerun for the tree and the tracker; everything below draws from it
try:
    ws = api_client.get_workspace()
except requests.RequestException:
    st.error(
        f"Backend not reachable at {api_client.API_BASE_URL}. "
        "Start it with `uv run uvicorn hiring_agent.api.main:app --reload`.",
        icon=":material/cloud_off:",
    )
    st.stop()

with st.sidebar:
    # uploads are saved here, before the main area is drawn, so new files show at once
    st.header("Files")
    if show_upload_form(ws["upload_types"]):
        ws = api_client.get_workspace()
    st.button(
        "Clear workspace",
        icon=":material/delete_sweep:",
        on_click=clear_everything,
        width="stretch",
        help="Deletes the job description, every resume, draft and tracker row.",
    )

    st.header("Agent")
    # the chat log sits above the input; it is filled at the end of the script
    chat_box = st.container()
    # every task starts by reading the JD, so there is nothing to do without one
    task = st.chat_input(
        "Describe a hiring task" if ws["has_job_description"] else "Upload a job description first",
        submit_mode="disable",
        disabled=not ws["has_job_description"],
    )

tree = ws["tree"]
show_kpis(tree)
show_last_run_changes(st.session_state.last_changes)

jd_column, resumes_column = st.columns([2, 3])
with jd_column:
    show_job_description(ws["has_job_description"])
with resumes_column:
    show_resumes(tree, st.session_state.last_changes)

show_drafts(tree.get("drafts", []))
show_tracker(ws["tracker"])

# Slow work goes last: run the agent after everything else is on screen.
with chat_box:
    show_history(st.session_state.history)
    if task:
        with st.chat_message("user"):
            st.write(task)
        with st.chat_message("assistant"):
            with st.spinner("The agent is working. A full run takes a minute or two."):
                entry = run_task(task)
        st.session_state.history.append(entry)
        st.rerun()
