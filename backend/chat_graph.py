"""chat_graph.py - LangGraph flow for the study chat and the compare chat.

 START -> input_guard --blocked--> END            (mask PII; refuse personal medical advice)
              | ok
        load_memory -> rewrite -> check_cache (exact, then semantic) --hit--> remember -> END
                                        | miss
        route_intent --general--> general_answer -> remember -> END      (2*2, hello, what is MACE)
              | study
   retrieve(top 30) -> rerank(LLM, top 6) -> compress (only when context+memory > 70% of budget)
        -> generate (answer + 3 follow-up questions) -> output_guard (numbers must be in the excerpts)
        -> save_cache (skipped if a number was not grounded) -> remember -> END

State holds user_id and the study ids, so retrieval is always limited to that user's own studies.
"""
import os
import time
from typing import List, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

import cache
import guards
import llm
import memory
import obs
import rag

RETRIEVE_K = int(os.getenv("RETRIEVE_K", "30"))
TOP_K = int(os.getenv("TOP_K", "6"))

SYSTEM_STUDY = """You answer questions about ONE clinical study, using ONLY the excerpts given.
- Cite the page for every fact like [p.7].
- Copy numbers exactly as in the excerpts. Never calculate or guess a number.
- If the excerpts do not contain the answer, say clearly that it is not in the paper (it may be in a supplement).
- Keep the answer short and clear (plain English, a few lines). Do not give medical advice.
- Also give up to 3 short follow-up questions the user could ask next, answerable from this paper."""

SYSTEM_COMPARE = """You compare several clinical studies, using ONLY the FACTS and the excerpts given.
- Cite every fact like [LABEL p.7] using the study label shown before each excerpt; FACTS lines are cited as [LABEL].
- Copy numbers exactly. Never calculate or guess a number. If a study does not report something, say so.
- Prefer a small table or short bullets for comparisons. Do not give medical advice.
- Also give up to 3 short follow-up questions the user could ask next."""

SYSTEM_GENERAL = ("You are a friendly assistant inside an app for reading clinical studies. Answer the user's general question "
                  "briefly and correctly. For anything about a medicine or health give general information only, never personal "
                  "medical advice, and say a doctor or pharmacist should be consulted. Do not mention any study.")

SYSTEM_ROUTER = ("Decide if the user's question is about a clinical study/paper or not. "
                 "'study' = anything about the study, trial, paper, its patients, drugs, doses, endpoints, results, numbers, "
                 "side effects, methods, or anything that could be answered from a paper. "
                 "'general' = clearly NOT about a paper: maths, greetings, chit-chat, general knowledge, "
                 "or a plain definition of a medical term. When unsure answer 'study'.")

NO_CONTEXT = ("I can't find this study's text. Chat needs the PDF to be uploaded and indexed "
              "(the demo study has no PDF). Please upload the PDF of the study.")


class Intent(BaseModel):
    intent: Literal["study", "general"]


class Ranked(BaseModel):
    indices: List[int]


class Compressed(BaseModel):
    excerpts: List[str]


class AnswerOut(BaseModel):
    answer: str
    follow_ups: List[str] = []


class ChatState(TypedDict, total=False):
    user_id: str
    scope: str                  # "study:<id>" or "compare"
    mem_scope: str              # where the memory is kept (compare chat: one memory per bucket)
    study_ids: List[str]
    labels: dict                # study_id -> short label (compare)
    facts: str                  # structured numbers from the database (compare)
    question: str
    standalone: str
    history: str
    cache_key: str
    candidates: List[dict]
    contexts: List[dict]
    answer: str
    follow_ups: List[str]
    sources: List[dict]
    from_cache: bool
    compressed: bool
    t0: float                   # when this question started (for the answer-time display)
    blocked: bool               # input_guard refused the question
    route: str                  # "study" or "general"
    scope_hash: str             # user + study tag for the semantic cache
    cache_kind: str             # "exact", "semantic" or "miss"
    sem_distance: float
    guard_flags: List[str]
    ungrounded: List[str]       # numbers in the answer that are not in the excerpts


# ---- helpers ----
def _tag(c: dict, s: ChatState) -> str:
    if s["scope"] == "compare":
        return f"{s.get('labels', {}).get(c['study_id'], 'Study')} p.{c['page']}"
    return f"p.{c['page']}"


def _sources(contexts: List[dict], s: ChatState) -> List[dict]:
    seen, out = set(), []
    for c in contexts:
        key = (c["study_id"], c["page"])
        if key not in seen:
            seen.add(key)
            out.append({"label": s.get("labels", {}).get(c["study_id"]) if s["scope"] == "compare" else None,
                        "page": c["page"]})
    return sorted(out, key=lambda x: (x["label"] or "", x["page"]))


# ---- nodes ----
def input_guard(s: ChatState) -> ChatState:
    """First node: hide PII, refuse personal medical advice. Everything else (even 2*2) passes."""
    q, kinds = guards.mask_pii(s["question"])
    flags = [f"pii:{k}" for k in kinds]
    out: ChatState = {"t0": time.time(), "question": q, "guard_flags": flags}
    if guards.is_personal_advice(q):
        out.update({"blocked": True, "answer": guards.REFUSAL_PERSONAL, "sources": [], "follow_ups": [],
                    "from_cache": False, "guard_flags": flags + ["personal_advice"]})
    return out


def load_memory(s: ChatState) -> ChatState:
    return {"history": memory.as_text(memory.load(s["user_id"], s.get("mem_scope", s["scope"])))}


_FOLLOW_START = ("and ", "also ", "what about", "how about", "why", "then ", "so ", "but ")
_FOLLOW_WORDS = {"it", "its", "that", "those", "this", "these", "they", "their", "them", "he", "she", "former", "latter", "same", "above"}


def _looks_followup(q: str) -> bool:
    low = q.lower().strip()
    words = set(low.replace("?", " ").replace(",", " ").split())
    return len(low.split()) <= 3 or low.startswith(_FOLLOW_START) or bool(words & _FOLLOW_WORDS)


def rewrite(s: ChatState) -> ChatState:
    """Turn a follow-up like 'and in the placebo arm?' into a stand-alone question.
    Self-contained questions are left as they are, so the same question always hits the cache."""
    q = s["question"]
    if not s.get("history") or not _looks_followup(q):
        return {"standalone": q}
    try:
        new = llm.text("Rewrite the user's last question so it can be understood without the conversation. "
                       "Keep names and numbers. Output only the question.",
                       f"Conversation:\n{s['history']}\n\nLast question: {q}").strip()
        return {"standalone": new or q}
    except Exception:
        return {"standalone": q}


def check_cache(s: ChatState) -> ChatState:
    scope_key = s["scope"] if s["scope"] != "compare" else "compare:" + ",".join(sorted(s["study_ids"]))
    key = cache.make_key(s["user_id"], scope_key, s["standalone"])
    hit = cache.get(key)                                                   # 1) exact match
    if hit:
        return {"cache_key": key, "answer": hit["answer"], "sources": hit["sources"],
                "follow_ups": hit.get("follow_ups", []), "from_cache": True, "cache_kind": "exact"}
    scope_hash = cache.make_scope(s["user_id"], scope_key)
    sem = cache.sem_get(scope_hash, s["standalone"])                       # 2) similar question, same user + study
    if sem:
        v, dist = sem
        return {"cache_key": key, "scope_hash": scope_hash, "answer": v["answer"], "sources": v["sources"],
                "follow_ups": v.get("follow_ups", []), "from_cache": True, "cache_kind": "semantic",
                "sem_distance": round(dist, 4)}
    return {"cache_key": key, "scope_hash": scope_hash, "from_cache": False, "cache_kind": "miss"}


def route_intent(s: ChatState) -> ChatState:
    """Cache miss only: is this a study question (RAG) or a general one (plain LLM)? On any doubt -> study."""
    try:
        return {"route": llm.structured_obj(Intent, SYSTEM_ROUTER, f"Question: {s['standalone']}").intent}
    except Exception:
        return {"route": "study"}


def general_answer(s: ChatState) -> ChatState:
    try:
        a = llm.text(SYSTEM_GENERAL, s["standalone"]).strip()
    except Exception:
        a = "Sorry, I could not answer that right now. Please try again."
    return {"answer": a, "sources": [], "follow_ups": []}


def retrieve(s: ChatState) -> ChatState:
    return {"candidates": rag.search_many(s["user_id"], s["study_ids"], s["standalone"], k=RETRIEVE_K)}


def rerank(s: ChatState) -> ChatState:
    """30 chunks -> the best TOP_K, judged by the LLM (one cheap call)."""
    cand = s.get("candidates") or []
    top = TOP_K + (2 if s["scope"] == "compare" else 0)
    if len(cand) <= top:
        return {"contexts": cand}
    listing = "\n".join(f"[{i}] ({_tag(c, s)}) {c['text'][:700]}" for i, c in enumerate(cand))
    try:
        r = llm.structured_obj(Ranked, f"Pick the {top} excerpts that best help answer the question, most useful first. "
                                       "Return their numbers in 'indices'.", f"Question: {s['standalone']}\n\n{listing}")
        idx = [i for i in dict.fromkeys(r.indices) if 0 <= i < len(cand)][:top]
        if idx:
            return {"contexts": [cand[i] for i in idx]}
    except Exception:
        pass
    return {"contexts": cand[:top]}               # fallback: search order


def _needs_compression(s: ChatState) -> bool:
    used = (memory.tokens(s.get("history", "")) + memory.tokens(s.get("facts", "")) +
            sum(memory.tokens(c["text"]) for c in s.get("contexts", [])))
    return used >= memory.THRESHOLD * memory.BUDGET


def _route_compress(s: ChatState) -> str:
    return "compress" if s.get("contexts") and _needs_compression(s) else "generate"


def compress(s: ChatState) -> ChatState:
    """Contextual compression: keep only the sentences of each excerpt that help answer (numbers verbatim)."""
    ctx = s["contexts"]
    listing = "\n\n".join(f"[{i}] {c['text']}" for i, c in enumerate(ctx))
    try:
        r = llm.structured_obj(Compressed, "For each numbered excerpt return ONLY the sentences or table lines that help answer "
                               "the question, copied exactly (do not change numbers). Same order, one string per excerpt, "
                               "empty string if nothing helps.", f"Question: {s['standalone']}\n\n{listing}")
        if len(r.excerpts) == len(ctx):
            return {"contexts": [{**c, "text": t} for c, t in zip(ctx, r.excerpts) if t.strip()], "compressed": True}
    except Exception:
        pass
    return {}


def generate(s: ChatState) -> ChatState:
    ctx = s.get("contexts") or []
    if not ctx:
        return {"answer": NO_CONTEXT, "sources": [], "follow_ups": []}
    blocks = "\n\n".join(f"[{_tag(c, s)}] {c['text']}" for c in ctx)
    facts = f"FACTS:\n{s['facts']}\n\n" if s.get("facts") else ""
    hist = f"Conversation so far:\n{s['history']}\n\n" if s.get("history") else ""
    system = SYSTEM_COMPARE if s["scope"] == "compare" else SYSTEM_STUDY
    out = llm.structured_obj(AnswerOut, system, f"{facts}{hist}Excerpts:\n{blocks}\n\nQuestion: {s['standalone']}")
    return {"answer": out.answer, "follow_ups": [q.strip() for q in out.follow_ups if q.strip()][:3],
            "sources": _sources(ctx, s)}


def output_guard(s: ChatState) -> ChatState:
    """Every number in the answer must be in the excerpts (or FACTS). If not: add a warning and do not cache."""
    ctx = s.get("contexts") or []
    if not ctx or s.get("answer") == NO_CONTEXT:
        return {"ungrounded": []}
    text = " ".join(c["text"] for c in ctx) + " " + (s.get("facts") or "")
    bad = guards.ungrounded_numbers(s["answer"], text, s.get("standalone", ""))
    if not bad:
        return {"ungrounded": []}
    return {"ungrounded": bad, "answer": s["answer"] + "\n\n" + guards.UNVERIFIED_NOTE,
            "guard_flags": s.get("guard_flags", []) + ["ungrounded_numbers"]}


def save_cache(s: ChatState) -> ChatState:
    if s.get("contexts") and not s.get("ungrounded"):     # never cache the "no context" message or an unverified answer
        idx_id = s["study_ids"][0] if s["scope"] != "compare" else "compare"     # so a study's answers can be wiped
        value = {"answer": s["answer"], "sources": s["sources"], "follow_ups": s["follow_ups"]}
        cache.put(s["cache_key"], value, s["user_id"], idx_id)
        cache.sem_put(value, s.get("scope_hash", ""), s["standalone"], s["user_id"], idx_id)
    return {}


def remember(s: ChatState) -> ChatState:
    if s.get("answer") and s["answer"] != NO_CONTEXT:
        memory.add_turn(s["user_id"], s.get("mem_scope", s["scope"]), {"q": s["question"], "a": s["answer"], "sources": s.get("sources", []),
                                                    "cached": bool(s.get("from_cache")), "follow_ups": s.get("follow_ups", []),
                                                    "ms": int((time.time() - s.get("t0", time.time())) * 1000)})
    return {}


def build_graph():
    g = StateGraph(ChatState)
    for name, fn in (("input_guard", input_guard), ("load_memory", load_memory), ("rewrite", rewrite),
                     ("check_cache", check_cache), ("route_intent", route_intent), ("general_answer", general_answer),
                     ("retrieve", retrieve), ("rerank", rerank), ("compress", compress),
                     ("generate", generate), ("output_guard", output_guard), ("save_cache", save_cache),
                     ("remember", remember)):
        g.add_node(name, fn)
    g.add_edge(START, "input_guard")
    g.add_conditional_edges("input_guard", lambda s: "blocked" if s.get("blocked") else "ok",
                            {"blocked": END, "ok": "load_memory"})
    g.add_edge("load_memory", "rewrite")
    g.add_edge("rewrite", "check_cache")
    g.add_conditional_edges("check_cache", lambda s: "hit" if s.get("from_cache") else "miss",
                            {"hit": "remember", "miss": "route_intent"})
    g.add_conditional_edges("route_intent", lambda s: "general" if s.get("route") == "general" else "study",
                            {"general": "general_answer", "study": "retrieve"})
    g.add_edge("general_answer", "remember")
    g.add_edge("retrieve", "rerank")
    g.add_conditional_edges("rerank", _route_compress, {"compress": "compress", "generate": "generate"})
    g.add_edge("compress", "generate")
    g.add_edge("generate", "output_guard")
    g.add_edge("output_guard", "save_cache")
    g.add_edge("save_cache", "remember")
    g.add_edge("remember", END)
    return g.compile()


_graph = None


def ask(user_id: str, question: str, study_ids: List[str], scope: str, labels: dict = None, facts: str = "",
        memory_scope: str = None) -> dict:
    global _graph
    if _graph is None:
        _graph = build_graph()
    out = _graph.invoke({"user_id": user_id, "scope": scope, "study_ids": study_ids, "labels": labels or {},
                         "facts": facts, "question": question.strip(), "mem_scope": memory_scope or scope,
                         "t0": time.time()})
    elapsed_ms = int((time.time() - out.get("t0", time.time())) * 1000)
    cand = out.get("candidates") or []
    scores = [c["score"] for c in cand if c.get("score") is not None]
    obs.event("chat", scope=("compare" if scope == "compare" else "study"), n_studies=len(study_ids),
              question_len=len(question), cache_hit=bool(out.get("from_cache")), n_candidates=len(cand),
              n_contexts=len(out.get("contexts") or []), top_score=round(max(scores), 3) if scores else None,
              compressed=bool(out.get("compressed")), no_context=out.get("answer") == NO_CONTEXT,
              answer_len=len(out.get("answer", "")), latency_ms=elapsed_ms,
              cache_kind=out.get("cache_kind"), sem_distance=out.get("sem_distance"), route=out.get("route"),
              blocked=bool(out.get("blocked")), guard_flags=out.get("guard_flags", []),
              ungrounded_numbers=len(out.get("ungrounded") or []))
    return {"answer": out["answer"], "sources": out.get("sources", []), "follow_ups": out.get("follow_ups", []),
            "cached": bool(out.get("from_cache")), "elapsed_ms": elapsed_ms}
