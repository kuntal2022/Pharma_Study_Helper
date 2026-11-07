"""memory.py - conversation memory per (user, chat scope), kept in Redis (falls back to process memory).

A scope is "study:<id>" for the chat next to a study, or "compare" for the main-page chat.
When the memory reaches 70% of MEMORY_BUDGET_TOKENS, the older turns are compressed into a short summary
(contextual compression of the conversation) and only the last 2 turns stay word for word.

.env (optional): MEMORY_BUDGET_TOKENS=4000
"""
import json
import os

import cache
import llm

BUDGET = int(os.getenv("MEMORY_BUDGET_TOKENS", "4000"))
THRESHOLD = 0.7
KEEP_TURNS = 2
TTL = 7 * 24 * 3600
_local = {}          # used when Redis is not running

SUMMARY_SYSTEM = ("You compress a chat about clinical studies. Write a short summary (max 150 words) of what the user "
                  "asked and what was found: keep study names, drug names, exact numbers and page references. No filler.")


def _key(user_id: str, scope: str) -> str:
    return f"mem:{user_id}:{scope}"


def tokens(text: str) -> int:
    return max(1, len(text or "") // 4)


def mem_tokens(m: dict) -> int:
    return tokens(m.get("summary", "")) + sum(tokens(t["q"]) + tokens(t["a"]) for t in m["turns"])


def load(user_id: str, scope: str) -> dict:
    k = _key(user_id, scope)
    try:
        v = cache._r().get(k)
        if v:
            return json.loads(v)
    except Exception:
        if k in _local:
            return _local[k]
    return {"summary": "", "turns": [], "compressions": 0}


def _save(user_id: str, scope: str, m: dict) -> None:
    k = _key(user_id, scope)
    _local[k] = m
    try:
        cache._r().set(k, json.dumps(m), ex=TTL)
    except Exception:
        pass


def as_text(m: dict) -> str:
    parts = []
    if m.get("summary"):
        parts.append("Summary of the earlier conversation: " + m["summary"])
    for t in m["turns"][-KEEP_TURNS - 1:]:
        parts.append(f"User: {t['q']}\nAssistant: {t['a']}")
    return "\n".join(parts)


def compact(m: dict) -> dict:
    older = m["turns"][:-KEEP_TURNS]
    if not older:
        return m
    body = (("Earlier summary: " + m["summary"] + "\n") if m.get("summary") else "") + \
        "\n".join(f"User: {t['q']}\nAssistant: {t['a']}" for t in older)
    try:
        m["summary"] = llm.text(SUMMARY_SYSTEM, body).strip()
        m["turns"] = m["turns"][-KEEP_TURNS:]
        m["compressions"] = m.get("compressions", 0) + 1
    except Exception:
        pass                                   # keep the full turns if the summary call fails
    return m


def add_turn(user_id: str, scope: str, turn: dict) -> dict:
    m = load(user_id, scope)
    m["turns"].append(turn)
    if mem_tokens(m) >= THRESHOLD * BUDGET:
        m = compact(m)
    _save(user_id, scope, m)
    return m


def reset(user_id: str, scope: str) -> None:
    k = _key(user_id, scope)
    _local.pop(k, None)
    try:
        cache._r().delete(k)
    except Exception:
        pass
