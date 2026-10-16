from logging import getLogger, StreamHandler, Formatter, INFO

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

# load OPENAI_API_KEY from .env
load_dotenv()

# model names are hardcoded for now; they move to a params file in Stage 4
PLANNER_MODEL = "gpt-6.1-sol"    # writes the plan
EXECUTOR_MODEL = "gpt-5.4-mini"  # runs each step and writes the final answer

# create the logger
logger = getLogger(name="Hiring Agent")
handler = StreamHandler()
logger.addHandler(handler)
logger.setLevel(INFO)
logger.propagate = False
handler.setFormatter(Formatter(fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

# shared llm clients -- temperature 0 so runs are as repeatable as possible.
# The planner is a reasoning model: it only accepts the default temperature (it rejects 0),
# and "low" reasoning effort keeps planning fast and cheap.
planner_llm = ChatOpenAI(model=PLANNER_MODEL, reasoning_effort="low")
llm = ChatOpenAI(model=EXECUTOR_MODEL, temperature=0)
