"""medicine_agent.py - "Medicine page": researches ONE medicine on the web (Tavily) and writes a structured profile.

LangGraph:   search (8 Tavily queries in parallel)  ->  write (LLM, only from the numbered sources)  ->  finalize (code checks)

.env: TAVILY_API_KEY, OPENAI_API_KEY (via llm.py)

Safety rules built in:
  * the LLM sees only the numbered search snippets; every block carries the source numbers it used (src);
    source numbers that do not exist are dropped in code; a block with no valid source is marked "unsourced"
  * videos / links are only URLs that Tavily returned (never invented)
  * prices are shown as found (with the page it came from) and labelled indicative
  * general information for professionals, not advice for an individual patient
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, TypedDict
from urllib.parse import parse_qs, urlparse

import requests
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

import llm

DISCLAIMER = ("General information collected from public web pages for professional reference. It is not medical advice and not a "
              "substitute for the approved prescribing information. Verify doses, prices and regulatory status with the official label "
              "and Medical Advisory / Regulatory before any use in promotion.")


# ------------------------------------------------------------------ schema
class Block(BaseModel):
    points: List[str] = []
    src: List[int] = []            # numbers of the sources used


class Brand(BaseModel):
    brand: str
    company: Optional[str] = None
    strength: Optional[str] = None
    price: Optional[str] = None    # exactly as found, with currency/pack, e.g. "Rs 245 for 10 tablets"
    src: Optional[int] = None


class StudyRef(BaseModel):
    title: str
    note: Optional[str] = None
    src: Optional[int] = None


class Profile(BaseModel):
    generic_name: str
    drug_class: Optional[str] = None
    overview: str = ""
    uses: Block = Block()
    moa_summary: str = ""
    moa_steps: Block = Block()               # 3-5 short steps of the mechanism, in order
    adverse_common: Block = Block()
    adverse_serious: Block = Block()
    contraindications: Block = Block()
    warnings: Block = Block()
    interactions: Block = Block()
    special_populations: Block = Block()     # pregnancy, kidney, liver, elderly, children
    patient_tips: Block = Block()            # patient safety: how to take, what to watch, when to call the doctor
    if_not_taken: Block = Block()            # what happens if the condition is left untreated
    if_stopped_or_missed: Block = Block()    # stopping suddenly / missed dose, as stated in sources
    brands: List[Brand] = []
    patent_regulatory: Block = Block()       # patent status, generics, approvals (FDA/EMA/CDSCO), prescription status
    compliance: Block = Block()              # schedule, pharmacovigilance, labelling / promotion cautions
    key_studies: List[StudyRef] = []


class QueryPlan(BaseModel):
    pass


class MState(TypedDict, total=False):
    name: str
    country: str
    results: dict          # query key -> list of {title,url,content}
    sources: list          # numbered: [{n,title,url}]
    profile: dict
    videos: list


# ------------------------------------------------------------------ tavily
def tavily(query: str, max_results: int = 5, domains: Optional[list] = None) -> list:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY is not set in .env")
    body = {"query": query, "search_depth": "basic", "max_results": max_results, "include_answer": False}
    if domains:
        body["include_domains"] = domains
    r = requests.post("https://api.tavily.com/search", json=body, headers={"Authorization": f"Bearer {key}"}, timeout=40)
    if r.status_code != 200:
        raise RuntimeError(f"Tavily error {r.status_code}: {r.text[:200]}")
    return [{"title": x.get("title", ""), "url": x.get("url", ""), "content": (x.get("content") or "")[:1000]} for x in r.json().get("results", [])]


def queries(name: str, country: str) -> dict:
    return {
        "uses": f"{name} indications uses dosage prescribing information",
        "safety": f"{name} adverse effects contraindications warnings precautions drug interactions",
        "moa": f"{name} mechanism of action pharmacology",
        "brands": f"{name} brand names price {country} company manufacturer",
        "patent": f"{name} patent status generic availability regulatory approval {country}",
        "untreated": f"{name} what happens if not taken untreated condition complications missed dose stopping suddenly",
        "studies": f"{name} landmark clinical trials efficacy safety results",
        "videos": f"{name} mechanism of action animation video",
    }


# ------------------------------------------------------------------ nodes
def search(s: MState) -> dict:
    qs = queries(s["name"], s.get("country") or "India")

    def one(item):
        k, q = item
        try:
            return k, tavily(q, 6 if k == "brands" else 5, ["youtube.com", "youtu.be"] if k == "videos" else None)
        except RuntimeError:
            raise
        except Exception:
            return k, []                  # one failed query must not kill the page

    with ThreadPoolExecutor(8) as ex:
        res = dict(ex.map(one, qs.items()))
    sources, seen = [], {}
    for k, rows in res.items():
        if k == "videos":
            continue
        for r in rows:
            if r["url"] and r["url"] not in seen and len(sources) < 40:
                seen[r["url"]] = len(sources) + 1
                sources.append({"n": len(sources) + 1, "title": r["title"], "url": r["url"], "content": r["content"]})
    if not sources:
        raise RuntimeError("No web results found for this medicine.")
    return {"results": res, "sources": sources}


WRITE_SYS = """You build a reference page about ONE medicine for pharma professionals, using ONLY the numbered SOURCES provided.
RULES:
- Every point must be supported by the sources; put the source numbers you used in that block's src list. If the sources do not say it, leave the block empty. Never use your own memory for facts, doses, prices or dates.
- brands: only brand names that appear in the sources, with company and strength if given; price exactly as written in the source (keep currency and pack size); no price -> null.
- Prices change: do not add any price that is not in a source.
- patent_regulatory: only what sources state (patent expiry, generics, approvals). If unclear say so in one point.
- if_not_taken: describe the risks of leaving the TREATED CONDITION untreated, as stated in sources; if_stopped_or_missed: what sources say about stopping suddenly or missing a dose. Do not give personal medical advice or doses for a patient.
- patient_tips: short safety tips from the sources (how it is taken, what to watch, when to see a doctor).
- Plain, short points (max ~25 words). moa_steps: 3-5 ordered short steps."""


def write(s: MState) -> dict:
    src_text = "\n".join(f"[{x['n']}] {x['title']} ({x['url']}): {x['content']}" for x in s["sources"])
    out = llm.structured_obj(Profile, WRITE_SYS, f"MEDICINE: {s['name']} (market: {s.get('country') or 'India'})\n\nSOURCES:\n{src_text}")
    return {"profile": out.model_dump()}


_BLOCKS = ["uses", "moa_steps", "adverse_common", "adverse_serious", "contraindications", "warnings", "interactions",
           "special_populations", "patient_tips", "if_not_taken", "if_stopped_or_missed", "patent_regulatory", "compliance"]


def _vid(url: str) -> Optional[str]:
    u = urlparse(url)
    if u.netloc.endswith("youtu.be"):
        return u.path.strip("/") or None
    if "youtube.com" in u.netloc and u.path == "/watch":
        return (parse_qs(u.query).get("v") or [None])[0]
    return None


def finalize(s: MState) -> dict:
    p, n_src = s["profile"], len(s["sources"])
    ok = lambda i: isinstance(i, int) and 1 <= i <= n_src
    for k in _BLOCKS:
        b = p[k]
        b["src"] = [i for i in b["src"] if ok(i)]
        b["unsourced"] = bool(b["points"]) and not b["src"]           # shown with a warning, not hidden
    for b in p["brands"]:
        b["src"] = b["src"] if ok(b["src"]) else None
    for b in p["key_studies"]:
        b["src"] = b["src"] if ok(b["src"]) else None
    drug = s["name"].lower().split()[0]
    videos, seen = [], set()
    for r in s["results"].get("videos", []):
        vid = _vid(r["url"])
        if vid and vid not in seen and (drug in r["title"].lower() or "mechanism" in r["title"].lower() or "moa" in r["title"].lower()):
            seen.add(vid)
            videos.append({"id": vid, "title": r["title"], "url": f"https://www.youtube.com/watch?v={vid}"})
    p["videos"] = videos[:4]
    p["sources"] = [{"n": x["n"], "title": x["title"], "url": x["url"]} for x in s["sources"]]
    p["disclaimer"] = DISCLAIMER
    return {"profile": p}


def _graph():
    g = StateGraph(MState)
    g.add_node("search", search); g.add_node("write", write); g.add_node("finalize", finalize)
    g.add_edge(START, "search"); g.add_edge("search", "write"); g.add_edge("write", "finalize"); g.add_edge("finalize", END)
    return g.compile()


GRAPH = _graph()


def research(name: str, country: str = "India") -> dict:
    name = " ".join(name.split())
    return GRAPH.invoke({"name": name, "country": country})["profile"]
