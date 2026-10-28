"""GET /health: are the two external APIs the agent needs (OpenAI and Jev) working?

Each service gets one tiny real call. A failed call is caught and reported as
"unhealthy" with its error; the endpoint itself never fails, so the frontend always
gets an answer.
"""

from fastapi import APIRouter
from langchain_openai import ChatOpenAI
from typesafe_sdk import Noul

from hiring_agent.app.clients import logger
from hiring_agent.app.guards import ask_jev

from hiring_agent.api.schemas import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])

# The cheapest current OpenAI model: the check only needs to know the API key and the
# service work, not how good the answer is. A short timeout and one retry keep a dead
# service from hanging the check.
HEALTH_CHECK_MODEL = "gpt-5.4-nano"
health_llm = ChatOpenAI(model=HEALTH_CHECK_MODEL, timeout=30, max_retries=1)

# one trivial question; any answer at all means Jev is up
JEV_HEALTH_STATE = {"text": "Hello"}
JEV_HEALTH_QUESTIONS = {"is_greeting": Noul(instructions="Is `text` a greeting?")}


def check_openai() -> str | None:
    """Return None if OpenAI answered, otherwise the error text."""
    try:
        health_llm.invoke("Hi")
        return None
    except Exception as error:
        logger.error(f"[health] OpenAI check failed: {error}")
        return f"{type(error).__name__}: {error}"


def check_jev() -> str | None:
    """Return None if Jev answered, otherwise the error text."""
    try:
        ask_jev(JEV_HEALTH_STATE, JEV_HEALTH_QUESTIONS)
        return None
    except Exception as error:  # TypeSafeError or a network error
        logger.error(f"[health] Jev check failed: {error}")
        return f"{type(error).__name__}: {error}"


@router.get("", response_model=HealthResponse)
def health() -> HealthResponse:
    openai_error = check_openai()
    jev_error = check_jev()
    return HealthResponse(
        openai="unhealthy" if openai_error else "healthy",
        openai_error=openai_error,
        jev="unhealthy" if jev_error else "healthy",
        jev_error=jev_error,
    )
