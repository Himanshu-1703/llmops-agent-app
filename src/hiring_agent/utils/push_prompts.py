from dotenv import load_dotenv
from langfuse import get_client

from hiring_agent.app.prompts import PROMPT_LABEL, PROMPT_TEXTS

# load the Langfuse keys from .env
load_dotenv()


def is_up_to_date(langfuse, name: str, text: str) -> bool:
    """True if the latest version in Langfuse already has exactly this text."""
    try:
        # cache_ttl_seconds=0 so we compare against what Langfuse has right now
        existing = langfuse.get_prompt(name=name, type="text", label=PROMPT_LABEL, cache_ttl_seconds=0)
    except Exception:
        # the prompt does not exist yet
        return False
    return existing.prompt == text


def push_prompts() -> None:
    langfuse = get_client()

    for name, text in PROMPT_TEXTS.items():
        if is_up_to_date(langfuse, name, text):
            print(f"{name}: unchanged, skipped")
            continue

        created = langfuse.create_prompt(name=name, type="text", prompt=text)
        print(f"{name}: created version {created.version}, labels={created.labels}")

    langfuse.flush()


if __name__ == "__main__":
    push_prompts()
