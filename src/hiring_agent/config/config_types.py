from pydantic import BaseModel


class AgentConfig(BaseModel):

    planner_model: str
    planner_reasoning_effort: str
    executor_model: str
    executor_temperature: float
    responder_model: str
    responder_temperature: float
    recursion_limit: int

    model_config = {"extra": "forbid"}


class PromptsConfig(BaseModel):

    label: str
    planner_prompt_name: str
    executor_prompt_name: str
    responder_prompt_name: str

    model_config = {"extra": "forbid"}


class Params(BaseModel):

    agent: AgentConfig
    prompts: PromptsConfig

    model_config = {"extra": "forbid"}
