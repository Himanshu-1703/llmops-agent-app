from logging import getLogger, StreamHandler, Formatter, INFO

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from hiring_agent.config.parameter_config import params_config

# load OPENAI_API_KEY from .env
load_dotenv()

# models and their settings come from params.yaml (validated on load)
agent_params = params_config.agent

# create the logger
logger = getLogger(name="Hiring Agent")
handler = StreamHandler()
logger.addHandler(handler)
logger.setLevel(INFO)
logger.propagate = False
handler.setFormatter(Formatter(fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

# shared llm clients -- executor and responder run at temperature 0 so runs are as repeatable as possible.
# The planner is a reasoning model: it only accepts the default temperature (it rejects 0),
# and "low" reasoning effort keeps planning fast and cheap.
# planner_llm writes the plan; llm runs each step; responder_llm writes the final answer
planner_llm = ChatOpenAI(model=agent_params.planner_model, reasoning_effort=agent_params.planner_reasoning_effort)
llm = ChatOpenAI(model=agent_params.executor_model, temperature=agent_params.executor_temperature)
responder_llm = ChatOpenAI(model=agent_params.responder_model, temperature=agent_params.responder_temperature)
