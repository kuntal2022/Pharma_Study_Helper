"""cache.py - Redis answer cache. Same user + same study + same question -> no LLM call.

.env:  REDIS_URL=redis://localhost:6379/0      (later: the Azure Cache for Redis URL, rediss://...)
       CACHE_TTL_SECONDS=86400                 (optional, default 1 day)
If Redis is not running, everything still works, just without caching.

Two layers (see chat_graph.check_cache):
  1) exact match   - hash of user + study + normalised question  (REDIS_URL, in-cluster Redis)
  2) semantic      - RedisVL SemanticCache on Azure Managed Redis, filtered to the same user + study
.env for layer 2:  REDIS_HOST=<name>.<region>.redis.azure.net   REDIS_KEY=<access key>
                   SEMANTIC_CACHE_DISTANCE_THRESHOLD=0.11        (lower = stricter; tuned in the demo notebook)
                   SEMANTIC_CACHE_ENABLED=true
Missing REDIS_HOST / REDIS_KEY (or any error) just switches layer 2 off.
"""
import hashlib
import json
import os
import re
import threading
from typing import Optional, Tuple
from urllib.parse import quote

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


# ---------------- semantic layer (Azure Managed Redis + RedisVL) ----------------
SEM_ENABLED = os.getenv("SEMANTIC_CACHE_ENABLED", "true").lower() == "true"
SEM_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_DISTANCE_THRESHOLD", "0.11"))
SEM_INDEX = os.getenv("SEMANTIC_CACHE_INDEX", "pharma_scoped_cache")
_sem = None
_sem_r = None
_sem_off = False
_sem_lock = threading.Lock()


def _sem_url() -> Optional[str]:
    host = os.getenv("REDIS_HOST", "").strip().split(":")[0]      # tolerate "host:10000"
    key = os.getenv("REDIS_KEY", "").strip()
    if not host or not key:
        return None
    return f"rediss://:{quote(key, safe='')}@{host}:{os.getenv('REDIS_PORT', '10000')}"


def make_scope(user_id: str, scope_key: str) -> str:
    """Tag value that limits a semantic hit to the same user + same study (or same compare set)."""
    return hashlib.sha256(f"{PROMPT_VERSION}|{user_id}|{scope_key}".encode()).hexdigest()[:32]


def _semantic():
    global _sem, _sem_r, _sem_off
    if _sem is not None or _sem_off or not SEM_ENABLED:
        return _sem
    with _sem_lock:
        if _sem is not None or _sem_off:
            return _sem
        url = _sem_url()
        if not url:
            _sem_off = True
            return None
        try:
            import redis
            from redisvl.extensions.cache.llm import SemanticCache
            from redisvl.utils.vectorize import HFTextVectorizer
            _sem = SemanticCache(
                name=SEM_INDEX, redis_url=url, distance_threshold=SEM_THRESHOLD,
                ttl=int(os.getenv("CACHE_TTL_SECONDS", "86400")),
                vectorizer=HFTextVectorizer(model="sentence-transformers/all-MiniLM-L6-v2"),
                filterable_fields=[{"name": "scope", "type": "tag"}])
            _sem_r = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=3)
        except Exception as e:                      # cache must never break the chat
            import obs
            obs.event("semantic_cache_off", error=type(e).__name__)
            _sem_off = True
            _sem = None
    return _sem


def _sidx(user_id: str, study_id: str) -> str:
    return f"sidx:{user_id}:{study_id}"


def sem_get(scope: str, question: str) -> Optional[Tuple[dict, float]]:
    """Nearest cached answer for this user+study within the distance threshold -> (value, distance), else None."""
    c = _semantic()
    if c is None:
        return None
    try:
        from redisvl.query.filter import Tag
        hits = c.check(prompt=question, num_results=1, filter_expression=Tag("scope") == scope)
        if hits:
            return json.loads(hits[0]["response"]), float(hits[0].get("vector_distance", 0.0))
    except Exception:
        pass
    return None


def sem_put(value: dict, scope: str, question: str, user_id: str, study_id: str) -> None:
    c = _semantic()
    if c is None:
        return
    try:
        k = c.store(prompt=question, response=json.dumps(value), filters={"scope": scope})
        if k:
            _sem_r.sadd(_sidx(user_id, study_id), k)        # so clear_study can wipe these too
    except Exception:
        pass


def clear_study(user_id: str, study_id: str) -> int:
    try:
        r = _r()
        keys = list(r.smembers(_idx(user_id, study_id)))
        if keys:
            r.delete(*keys)
        r.delete(_idx(user_id, study_id))
        n = len(keys)
    except Exception:
        n = 0
    try:                                                   # semantic entries of this study
        if _semantic() is not None:
            skeys = list(_sem_r.smembers(_sidx(user_id, study_id)))
            if skeys:
                _sem_r.delete(*skeys)
            _sem_r.delete(_sidx(user_id, study_id))
            n += len(skeys)
    except Exception:
        pass
    return n
