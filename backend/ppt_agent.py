"""ppt_agent.py - slide-deck agent with a human in the loop (LangGraph).

Conversation (one session per deck, the human can repeat step 2 until happy, max 10 slides):

  start     : scan the studies -> ~15 candidate points -> plan 6 slides (default) -> ASK the human
  revise    : human says "10 slides, I need X and Y" (and/or typed a note on a slide)
              -> rescan with the human's points in focus -> plan = human's points + the best scanned points -> ASK again
  approve   : human approves (or the 60 minute timer ran out = "timeout": go with the current plan, the agent's best choice)
              -> write every slide -> number check -> deck (same shape as marketing.generate, so the .pptx export is shared)

Rules: slide 1 is always the title/overview. Total slides 4..10, default 6.
A note the human types on a slide is ADDED to the agent's own choice for that slide (never replaces it);
an empty note = the agent's best choice.
Facts come from the database (marketing.build_facts); the LLM never invents numbers (checked in code).
"""
import time
import uuid
from typing import Dict, List, Literal, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

import llm
import marketing
from marketing import Deck, RULES, build_facts, unverified_numbers

MIN_SLIDES, DEFAULT_SLIDES, MAX_SLIDES = 4, 6, 10
TIMER_SECONDS = 60 * 60          # human has 60 minutes to reply; then the agent creates its own plan
TTL = 4 * 3600
SESSIONS: Dict[str, dict] = {}


# ------------------------------------------------------------------ schemas
class Cand(BaseModel):
    title: str
    fact: str                    # exact numbers from FACTS with study label
    why: str                     # why it matters to a doctor (one line)
    layout: Literal["flow", "kpi", "cards", "bullets"] = "bullets"


class Scan(BaseModel):
    candidates: List[Cand]       # best first


class PlanSlide(BaseModel):
    title: str
    focus: str                   # what this slide will say (one or two lines, facts included)
    layout: Literal["title", "flow", "kpi", "cards", "bullets"] = "bullets"
    from_human: bool = False     # True when it exists because the human asked for it
    human_note: str = ""         # the note the human typed on this slide (kept as typed)


class Plan(BaseModel):
    slides: List[PlanSlide]
    message: str                 # 2-3 lines to the human: what the agent will put and what it left out


class PState(TypedDict, total=False):
    facts: str
    studies: List[str]
    action: str
    n: int
    wishes: List[str]
    cands: List[dict]
    plan: List[dict]
    message: str
    deck: dict


# ------------------------------------------------------------------ nodes
SCAN_SYS = RULES + "\nYou are scanning studies to find the best slide-worthy points."


def scan(s: PState) -> dict:
    focus = ("\nThe human especially wants: " + "; ".join(s["wishes"]) + ". Include a candidate for each if the FACTS support it.") if s.get("wishes") else ""
    out = llm.structured_obj(Scan, SCAN_SYS,
                             f"FACTS:\n{s['facts']}\n\nFind the 15 best points for a doctor-facing deck, best first (design, efficacy, key secondary outcomes, safety, subgroups, limitations, takeaway)."
                             f" Each: title, fact (exact numbers + study label), why it matters, layout (flow=design steps, kpi=big numbers, cards=3 findings, bullets).{focus}")
    cands = [c.model_dump() for c in out.candidates][:15]
    old = s.get("cands") or []
    seen = {c["title"].lower() for c in cands}
    cands += [c for c in old if c["title"].lower() not in seen]          # a rescan only ADDS to what was found before
    return {"cands": cands[:20]}


def plan(s: PState) -> dict:
    n = max(MIN_SLIDES, min(MAX_SLIDES, int(s.get("n") or DEFAULT_SLIDES)))
    prev = s.get("plan") or []
    out = llm.structured_obj(
        Plan, RULES,
        f"CANDIDATE POINTS (best first):\n{[{k: v for k, v in c.items()} for c in s['cands']]}\n\n"
        f"HUMAN'S POINTS (must all appear as their own slide, unless FACTS do not contain them - then say so in the message and do NOT invent): {s.get('wishes') or 'none'}\n"
        f"PREVIOUS PLAN (keep every slide that has a human_note, keep its note exactly): {prev or 'none'}\n\n"
        f"Plan exactly {n} slides. Slide 1 = title/overview (layout title). Then the human's points first, then the best candidate points to fill the rest. "
        f"message: 2-3 short lines telling the human what you will put and what you left out.")
    slides = [p.model_dump() for p in out.slides][:n]
    if not slides or slides[0]["layout"] != "title":
        slides.insert(0, PlanSlide(title="Overview", focus="Study name, population, key message", layout="title").model_dump())
        slides = slides[:n]
    return {"plan": slides, "message": out.message, "n": len(slides)}


def build(s: PState) -> dict:
    p = s["plan"]
    spec = "\n".join(f"Slide {i + 1} [{x['layout']}] {x['title']}: {x['focus']}"
                     + (f"  | HUMAN ASKED TO ALSO COVER: {x['human_note']}" if x.get("human_note") else "") for i, x in enumerate(p))
    out = llm.structured_obj(
        Deck, RULES,
        f"FACTS:\n{s['facts']}\n\nWrite EXACTLY {len(p)} slides following this plan, in this order and with these layouts. "
        f"Where a human note is given, cover the note IN ADDITION to the plan for that slide (if FACTS do not support it, say 'not reported in the study' - never invent).\n"
        f"Layout rules: title = title + 1-2 bullets; flow = steps (3-5 short steps); kpi = kpis (3-4 of value/label/tone); cards = 3 short bullets; bullets = 3-5 bullets. 1-2 lines of speaker notes each.\n{spec}")
    data = out.model_dump()
    data["slides"] = data["slides"][:len(p)]
    return {"deck": {"kind": "slides", "content": data, "warnings": unverified_numbers(data, s["facts"]),
                     "studies": s["studies"], "note": marketing.REVIEW_NOTE}}


def _route(s: PState) -> str:
    a = s.get("action")
    return "build" if a in ("approve", "timeout") else ("scan" if a in ("start", "revise") else END)


def _graph():
    g = StateGraph(PState)
    g.add_node("scan", scan); g.add_node("plan", plan); g.add_node("build", build)
    g.add_conditional_edges(START, _route, {"scan": "scan", "build": "build", END: END})
    g.add_edge("scan", "plan"); g.add_edge("plan", END); g.add_edge("build", END)
    return g.compile()


GRAPH = _graph()


# ------------------------------------------------------------------ session api (called by api.py)
def _gc():
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if now - v["ts"] > TTL]:
        SESSIONS.pop(k, None)


def _public(sid: str, st: dict) -> dict:
    return {"sid": sid, "plan": st["plan"], "message": st["message"], "n": st["n"], "found": len(st["cands"]),
            "min": MIN_SLIDES, "max": MAX_SLIDES, "timer_seconds": TIMER_SECONDS}


def start(user_id: str, items) -> dict:
    _gc()
    st = GRAPH.invoke({"action": "start", "facts": build_facts(items), "studies": [l for l, _ in items],
                       "n": DEFAULT_SLIDES, "wishes": [], "cands": [], "plan": []})
    sid = uuid.uuid4().hex
    SESSIONS[sid] = {"user": user_id, "ts": time.time(), "state": st}
    return _public(sid, st)


def _session(sid: str, user_id: str) -> dict:
    sess = SESSIONS.get(sid)
    if not sess or sess["user"] != user_id:
        raise KeyError("Session expired. Start again.")
    sess["ts"] = time.time()
    return sess


def _apply_notes(st: dict, notes: Optional[Dict[str, str]]):
    for k, v in (notes or {}).items():
        try:
            i = int(k)
        except ValueError:
            continue
        if 0 <= i < len(st["plan"]):
            st["plan"][i]["human_note"] = (v or "").strip()[:500]


def revise(user_id: str, sid: str, n: Optional[int], request: str, notes: Optional[Dict[str, str]]) -> dict:
    sess = _session(sid, user_id); st = sess["state"]
    _apply_notes(st, notes)
    request = (request or "").strip()[:800]
    if request:
        st["wishes"] = (st.get("wishes") or []) + [request]
    if n:
        st["n"] = max(MIN_SLIDES, min(MAX_SLIDES, int(n)))
    st["action"] = "revise"
    sess["state"] = st = GRAPH.invoke(st)
    return _public(sid, st)


def approve(user_id: str, sid: str, notes: Optional[Dict[str, str]], timed_out: bool = False) -> dict:
    sess = _session(sid, user_id); st = sess["state"]
    _apply_notes(st, notes)
    st["action"] = "timeout" if timed_out else "approve"
    out = GRAPH.invoke(st)
    SESSIONS.pop(sid, None)
    return out["deck"]
