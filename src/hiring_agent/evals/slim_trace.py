"""Shrink the DeepEval trace before a metric's judge reads it.

The CallbackHandler saves the full input and output of every LangGraph step, so the same
message history is copied into every node. One small run is already ~30k tokens, and each
metric sends the whole trace to the judge. slim_trace() keeps each fact once:

  - root span:               input + output  (the task and the final answer)
  - tool spans:              input + output  (what really happened)
  - llm spans and the
    planner/executor/responder nodes: output only (the plan and each step's result)
  - every other node:        name, type and position in the tree only
  - routing spans (more_steps, tools_condition): dropped

The agent and the recorded trace are not changed; only the copy the judge reads is.
"""

import copy
import json
from pathlib import Path
import re

NODES_WITH_OUTPUT = {"planner", "executor", "responder"}
ROUTING_NODES = {"more_steps", "tools_condition"}
SLIM_FLAG = "_slim"


def slim_trace(span: dict, is_root: bool = True) -> dict:
    """Return a slimmed copy of one span and (recursively) its children."""
    slim = {k: v for k, v in span.items() if k not in ("input", "output", "children")}

    span_type = span.get("type")
    if is_root or span_type == "tool":
        slim["input"] = span.get("input")
        slim["output"] = span.get("output")
    elif span_type == "llm" or span.get("name") in NODES_WITH_OUTPUT:
        slim["output"] = span.get("output")

    slim["children"] = [
        slim_trace(child, is_root=False)
        for child in span.get("children") or []
        if child.get("name") not in ROUTING_NODES
    ]
    if is_root:
        slim[SLIM_FLAG] = True
    return slim


def _save(raw: dict, slim: dict, save_dir) -> None:
    """Write the full and the slim trace so we can see exactly what the judge read."""
    task = str((raw.get("input") or {}).get("task", "")) if isinstance(raw.get("input"), dict) else str(raw.get("input"))
    name = re.sub(r"[^a-z0-9]+", "_", task.lower()).strip("_")[:60] or "trace"
    folder = Path(save_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.raw.json").write_text(json.dumps(raw, indent=2, default=str))
    (folder / f"{name}.judge.json").write_text(json.dumps(slim, indent=2, default=str))


def with_slim_trace(metric_cls, save_dir=None):
    """Return a subclass of a DeepEval trace metric that judges the slim trace.

    The slim trace is swapped in only while the metric runs, then the full trace is put
    back, so metrics wrapped differently can share one test case.
    """

    class SlimTraceMetric(metric_cls):
        def _swap_in_slim(self, test_case):
            raw = getattr(test_case, "_trace_dict", None)
            if not isinstance(raw, dict) or raw.get(SLIM_FLAG):
                return None  # no trace, or already slim (measure -> a_measure)
            slim = slim_trace(copy.deepcopy(raw))
            if save_dir:
                _save(raw, slim, save_dir)
            test_case._trace_dict = slim
            return raw

        def measure(self, test_case, *args, **kwargs):
            raw = self._swap_in_slim(test_case)
            try:
                return super().measure(test_case, *args, **kwargs)
            finally:
                if raw is not None:
                    test_case._trace_dict = raw

        async def a_measure(self, test_case, *args, **kwargs):
            raw = self._swap_in_slim(test_case)
            try:
                return await super().a_measure(test_case, *args, **kwargs)
            finally:
                if raw is not None:
                    test_case._trace_dict = raw

    # DeepEval looks up judge prompts by class name, so keep the original name
    SlimTraceMetric.__name__ = metric_cls.__name__
    SlimTraceMetric.__qualname__ = metric_cls.__qualname__
    return SlimTraceMetric
