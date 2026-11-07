"""llm.py - the only place that talks to the chat model (so tests can replace it).

.env: OPENAI_API_KEY, OPENAI_CHAT_MODEL (default gpt-4.1-mini)
"""
import json
import os

from dotenv import load_dotenv

load_dotenv(override=True)


def get_llm():
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini"),
                      api_key=os.environ["OPENAI_API_KEY"], temperature=0)


def text(system: str, human: str) -> str:
    return get_llm().invoke([("system", system), ("human", human)]).content


def structured_obj(schema, system: str, human: str):
    """Strict: returns a pydantic object of `schema` (raises if the model output is invalid)."""
    return get_llm().with_structured_output(schema).invoke([("system", system), ("human", human)])


def structured_dict(schema, system: str, human: str) -> dict:
    """Lenient: returns a plain dict. If strict parsing fails we still return the raw JSON the model
    produced, so the caller can drop only the bad rows instead of losing everything."""
    r = get_llm().with_structured_output(schema, include_raw=True).invoke([("system", system), ("human", human)])
    if r.get("parsed") is not None:
        return r["parsed"].model_dump()
    raw = r["raw"]
    if getattr(raw, "tool_calls", None):
        return raw.tool_calls[0]["args"]
    return json.loads(raw.content)
