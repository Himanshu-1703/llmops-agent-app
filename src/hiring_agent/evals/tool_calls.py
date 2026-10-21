"""Record the tool calls of one agent run, ready for DeepEval's ToolCorrectnessMetric.

ToolCorrectness compares the calls the agent made (tools_called) with the calls in the
golden (expected_tools): same tools, same order, same arguments. That only works for
arguments that have ONE right answer, so every call is cleaned first:

    - free-text arguments are dropped (notes, subject, body): the LLM words them
      differently every run, so an exact match would always fail
    - nice_to_haves is sorted ("Docker;AWS" -> "AWS;Docker"): the order does not
      matter, a wrong or missing skill still does
    - lookup reads are dropped (list_files, read_file('candidates.csv')): the executor
      sometimes checks these on its own; they do not change the result

The same clean_tool_call() is applied to the golden's expected_tools, so both sides are
compared in the same shape.
"""

from deepeval.test_case import ToolCall
from langchain_core.callbacks import BaseCallbackHandler

FREE_TEXT_ARGS = {"notes", "subject", "body"}


def _is_lookup(name: str, args: dict) -> bool:
    return name == "list_files" or (name == "read_file" and args.get("path") == "candidates.csv")


def clean_tool_call(name: str, args: dict) -> ToolCall | None:
    """The call as ToolCorrectness should see it, or None if it is a lookup read."""
    args = args or {}
    if _is_lookup(name, args):
        return None

    kept = {k: v for k, v in args.items() if k not in FREE_TEXT_ARGS}
    if "nice_to_haves" in kept:
        skills = [s.strip() for s in str(kept["nice_to_haves"]).split(";") if s.strip()]
        kept["nice_to_haves"] = ";".join(sorted(skills))
    return ToolCall(name=name, input_parameters=kept)


def clean_tool_calls(calls: list[ToolCall]) -> list[ToolCall]:
    """Clean a list of ToolCalls, e.g. a golden's expected_tools."""
    cleaned = [clean_tool_call(c.name, c.input_parameters) for c in calls or []]
    return [c for c in cleaned if c is not None]


class ToolCallRecorder(BaseCallbackHandler):
    """LangChain callback: collects every tool call of one run, in order, already cleaned.

    Pass it next to DeepEval's CallbackHandler: run_agent(..., callbacks=[..., recorder]).
    LangChain hands callbacks down to the executor's sub-agent, so its tool calls are
    seen too. Use a NEW recorder for every golden.
    """

    def __init__(self):
        self.tool_calls: list[ToolCall] = []

    def on_tool_start(self, serialized, input_str, *, inputs=None, **kwargs):
        call = clean_tool_call(serialized["name"], inputs)
        if call is not None:
            self.tool_calls.append(call)
