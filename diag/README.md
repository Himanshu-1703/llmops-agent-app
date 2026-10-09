# Agent graphs

The two LangGraph graphs from `src/hiring_agent/app/agent.py`, rendered with
`get_graph().draw_mermaid_png()`. Solid arrows are fixed edges; dotted arrows are
conditional edges.

## Main agent graph

planner runs once, executor repeats once per plan step, responder writes the final answer.

![Main agent graph](agent_graph.png)

## Executor sub-agent graph

The `create_agent` ReAct loop that runs inside ONE executor visit: model decides, tools
run, and the loop ends when the model makes no tool call.

![Executor sub-agent graph](executor_subagent_graph.png)
