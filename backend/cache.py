"""cache.py - Redis answer cache. Same user + same study + same question -> no LLM call.

.env:  REDIS_URL=redis://localhost:6379/0      (later: the Azure Cache for Redis URL, rediss://...)
       CACHE_TTL_SECONDS=86400                 (optional, default 1 day)
If Redis is not running, everything still works, just without caching.
"""
import hashlib
import json
import os
import re
from typing import Optional

from dotenv import load_dotenv

load_dotenv(override=True)

PROMPT_VERSION = "v1"          # change this to invalidate every cached answer after a prompt change
_client = None


def _r():
    global _client
    if _client is None:
        import redis
        _client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"),
                                       decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
    return _client


def make_key(user_id: str, study_id: str, question: str) -> str:
    norm = re.sub(r"\s+", " ", question.strip().lower()).strip(" ?!.")
    h = hashlib.sha256(f"{PROMPT_VERSION}|{user_id}|{study_id}|{norm}".encode()).hexdigest()
    return f"ans:{h}"


def _idx(user_id: str, study_id: str) -> str:
    return f"idx:{user_id}:{study_id}"


def get(key: str) -> Optional[dict]:
    try:
        v = _r().get(key)
        return json.loads(v) if v else None
    except Exception:
        return None


def put(key: str, value: dict, user_id: str, study_id: str) -> None:
    try:
        r = _r()
        r.set(key, json.dumps(value), ex=int(os.getenv("CACHE_TTL_SECONDS", "86400")))
        r.sadd(_idx(user_id, study_id), key)          # so we can wipe a study's answers on delete
    except Exception:
        pass


def clear_study(user_id: str, study_id: str) -> int:
    try:
        r = _r()
        keys = list(r.smembers(_idx(user_id, study_id)))
        if keys:
            r.delete(*keys)
        r.delete(_idx(user_id, study_id))
        return len(keys)
    except Exception:
        return 0
