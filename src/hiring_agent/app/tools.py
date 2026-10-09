"""The 7 tools the executor can call.

Each tool is a thin wrapper around a function in workspace.py: it calls it and turns
the result into a short text message. The docstring is what the model reads, so it
says what the tool does AND what it does not do.

Tools never raise. Any problem is returned as a string starting with "Error:", so
the model can read it and carry on.
"""

from langchain_core.tools import tool

from hiring_agent.app import workspace


@tool
def list_files(folder: str = "") -> str:
    """List the files and subfolders directly inside one workspace folder (the
    workspace root if folder is empty). Subfolders end with '/'. It does not show
    file contents and does not look inside subfolders."""
    try:
        entries = workspace.list_folder(folder)
    except ValueError as error:
        return f"Error: {error}"

    name = f"'{folder}'" if folder else "the workspace root"
    if not entries:
        return f"{name} is empty."
    lines = "\n".join(f"- {entry}" for entry in entries)
    return f"Contents of {name} ({len(entries)} items):\n{lines}"


@tool
def read_file(path: str) -> str:
    """Read the full text of one workspace file, such as a resume, the job
    description or a draft. It reads one file at a time and does not change anything."""
    try:
        text = workspace.read_text(path)
    except ValueError as error:
        return f"Error: {error}"
    return f"Contents of '{path}':\n{text}"


@tool
def add_candidate(
    name: str,
    email: str,
    years_experience: int,
    must_haves_met: str,
    nice_to_haves: str,
    fit_score: int,
    decision: str,
    resume_file: str,
    notes: str = "",
) -> str:
    """Add ONE candidate as a new row in the candidates.csv tracker and return the
    new candidate_id. must_haves_met is 'yes' or 'no'. nice_to_haves is the matched
    nice-to-have skills separated by semicolons (empty if none). fit_score is a whole
    number from 0 to 6. decision is 'shortlisted' or 'rejected'. email is empty if the
    resume has none. It refuses a candidate whose name is already in the tracker.
    It does not move the resume file and does not edit existing rows."""
    try:
        candidate_id = workspace.add_tracker_row(
            name, email, years_experience, must_haves_met, nice_to_haves,
            fit_score, decision, resume_file, notes,
        )
    except ValueError as error:
        return f"Error: {error}"
    return f"Added {candidate_id}: {name} ({decision}, fit score {fit_score})"


@tool
def draft_email(to: str, subject: str, body: str) -> str:
    """Save an email draft as a text file in drafts/. It only saves a draft: it
    never sends an email. It refuses an empty recipient, so do not call it for a
    candidate who has no email address."""
    try:
        path = workspace.write_draft(to, subject, body)
    except ValueError as error:
        return f"Error: {error}"
    return f"Draft saved to '{path}' (to: {to}, subject: {subject})"


@tool
def update_candidate(candidate_id: str, field: str, value: str) -> str:
    """Change ONE field of an existing tracker row, for example
    update_candidate('C-001', 'decision', 'rejected'). It cannot change the
    candidate_id and cannot add or delete rows."""
    try:
        workspace.update_tracker_field(candidate_id, field, value)
    except ValueError as error:
        return f"Error: {error}"
    return f"Updated {candidate_id}: {field} = '{value}'"


@tool
def move_file(source: str, destination: str) -> str:
    """Move or rename ONE file inside the workspace. If destination is a folder
    (for example 'shortlisted/'), the file keeps its name. It does not create new
    folders, does not overwrite an existing file and does not move folders."""
    try:
        new_path = workspace.move(source, destination)
    except ValueError as error:
        return f"Error: {error}"
    return f"Moved '{source}' to '{new_path}'"


@tool
def delete_file(path: str) -> str:
    """Delete ONE file from the workspace. It never deletes folders and cannot be
    undone."""
    try:
        workspace.delete(path)
    except ValueError as error:
        return f"Error: {error}"
    return f"Deleted '{path}'"


TOOLS = [
    list_files, read_file, add_candidate, draft_email,
    update_candidate, move_file, delete_file,
]


def _describe(tool_obj) -> str:
    """One line for the planner: name(args): description."""
    args = []
    for arg_name, schema in tool_obj.args.items():
        # show defaults so the planner knows which arguments are optional
        args.append(f"{arg_name}={schema['default']!r}" if "default" in schema else arg_name)
    description = " ".join(tool_obj.description.split())
    return f"- {tool_obj.name}({', '.join(args)}): {description}"


# the planner has no tools bound to it, so it reads this text instead
TOOL_LIST = "\n".join(_describe(t) for t in TOOLS)
