"""Streamlit frontend (Stage 1): the workspace view plus the agent panel.

The agent is imported and invoked directly, in the same process (no HTTP yet; the
FastAPI layer comes in Stage 6).

Run from the repo root:
    uv run streamlit run src/hiring_agent/frontend/agent_app.py
"""

import pandas as pd
import streamlit as st

from hiring_agent.app import workspace
from hiring_agent.app.agent import build_agent, run_agent

st.set_page_config(
    page_title="Hiring assistant",
    page_icon=":material/work:",
    layout="wide",
)

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


@st.cache_resource
def get_agent():
    """Build the graph once per server process and reuse it for every task."""
    return build_agent()


# --------------------------------------------------------------------------
# What changed in the workspace during a run
# --------------------------------------------------------------------------

def take_snapshot() -> dict:
    """Every file path and every tracker id, taken before and after a run."""
    files = {path for paths in workspace.file_tree().values() for path in paths}
    tracker_ids = {row["candidate_id"] for row in workspace.read_tracker()}
    return {"files": files, "tracker_ids": tracker_ids}


def diff_snapshots(before: dict, after: dict) -> dict:
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
    return {"added": added, "moved": moved, "deleted": deleted, "new_rows": new_rows}


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


def show_job_description() -> None:
    """Always visible: the screening criteria the agent works from."""
    with st.container(border=True):
        st.markdown("**:material/work: Job description**")
        with st.container(height=BOX_HEIGHT, border=False):
            st.markdown(smaller_headings(workspace.read_text("job_description.md")))


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
            st.caption("No resumes in the workspace.")
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
                st.code(workspace.read_text(selected), language="text", wrap_lines=True)


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
            to, subject, body = split_draft(workspace.read_text(selected))
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
                    "Fit score", min_value=0, max_value=6, format="%d"
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
# Agent panel (sidebar)
# --------------------------------------------------------------------------

def reset_everything() -> None:
    """Button callback: rebuild the workspace and forget the previous runs."""
    workspace.reset_workspace()
    st.session_state.history = []
    st.session_state.last_changes = None
    st.session_state.selected_resume = None
    st.session_state.selected_draft = None


def run_task(task: str) -> dict:
    """Run the agent on one task and record what it did to the workspace."""
    if not workspace.WORKSPACE_DIR.exists():
        return {"task": task, "error": "There is no workspace yet. Click Reset workspace first."}

    before = take_snapshot()
    try:
        state = run_agent(get_agent(), task, full=True)
    except Exception as error:  # show the failure in the panel instead of crashing the page
        return {"task": task, "error": f"{type(error).__name__}: {error}"}

    changes = diff_snapshots(before, take_snapshot())
    st.session_state.last_changes = changes
    return {
        "task": task,
        "response": state["response"],
        "plan": state["plan"],
        "past_steps": state["past_steps"],
        "changes": changes,
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

st.title("Hiring assistant", icon=":material/work:")

with st.sidebar:
    st.header("Agent")
    st.button(
        "Reset workspace",
        icon=":material/refresh:",
        on_click=reset_everything,
        width="stretch",
        help="Rebuilds every file and the tracker from the seed data.",
    )

    # the chat log sits above the input; it is filled at the end of the script
    chat_box = st.container()
    task = st.chat_input("Describe a hiring task", submit_mode="disable")

# The workspace folder only exists after the first reset.
if workspace.WORKSPACE_DIR.exists():
    tree = workspace.file_tree()
    show_kpis(tree)
    show_last_run_changes(st.session_state.last_changes)

    jd_column, resumes_column = st.columns([2, 3])
    with jd_column:
        show_job_description()
    with resumes_column:
        show_resumes(tree, st.session_state.last_changes)

    show_drafts(tree.get("drafts", []))
    show_tracker(workspace.read_tracker())
else:
    st.info(
        "No workspace yet. Click **Reset workspace** in the sidebar to create it.",
        icon=":material/info:",
    )

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
