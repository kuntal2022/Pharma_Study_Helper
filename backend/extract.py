"""extract.py - PDF bytes -> StudyUI (structured), any study design.

Fast: 1 small 'core' call (design, arms, stats, conclusions), then 3 calls IN PARALLEL
      (outcomes / baseline / safety) that already know the arm names.
Safe: rows that fail validation are dropped and listed in not_reported - one bad row never fails the upload.
Needs in .env: OPENAI_API_KEY, OPENAI_CHAT_MODEL (default gpt-4.1-mini)
"""
import os
from concurrent.futures import ThreadPoolExecutor
from typing import List

import pymupdf
from dotenv import load_dotenv
from pydantic import BaseModel

import llm
from models import (AdverseEvent, Arm, BaselineSection, Conclusions, ContinuousOutcome, DeathSummary,
                    Disposition, OutcomeResult, Population, StatisticalPlan, StudyDesign, StudyUI,
                    SubgroupResult, validate_lenient)

load_dotenv(override=True)
MAX_CHARS = 450_000

RULES = """Rules:
- Copy numbers EXACTLY as printed. Never calculate, estimate or round. If a value is not printed, use null.
- Text fields: a short paraphrase in your own words, except names and p_value_text which are copied.
- Every row needs source_page = the page number from the [PAGE n] marker where you found it.
- The paper can be any design (RCT, cohort, review, meta-analysis, single-arm). If something does not apply, use null or an empty list.
  Never invent a comparison that is not in the paper."""


# ---- the four partial schemas ----
class Core(BaseModel):
    design: StudyDesign
    population: Population = Population()
    arms: List[Arm]
    disposition: Disposition = Disposition()
    statistics: StatisticalPlan = StatisticalPlan()
    conclusions: Conclusions = Conclusions()
    supplement_refs: List[str] = []
    not_reported: List[str] = []


class OutcomesPart(BaseModel):
    outcomes: List[OutcomeResult] = []
    continuous_outcomes: List[ContinuousOutcome] = []
    subgroups: List[SubgroupResult] = []


class BaselinePart(BaseModel):
    baseline: BaselineSection


class SafetyPart(BaseModel):
    adverse_events: List[AdverseEvent] = []
    deaths: List[DeathSummary] = []


CORE_PROMPT = f"""Extract the study design, population, treatment arms, patient disposition, statistical plan and the authors' conclusions.
- arms: one entry per treatment arm (main drug, comparator or placebo), with the arm name exactly as the paper uses it.
- key_message_one_line: one plain sentence using only facts from the paper.
- not_reported: what the paper does not give. supplement_refs: tables or figures that are only in a supplement.
{RULES}"""

OUTCOMES_PROMPT = """Extract every efficacy and safety OUTCOME with an effect size: primary, secondary, components, subgroups,
and continuous outcomes (mean differences). One row per outcome AND per analysis set (ITT and per-protocol are separate rows).
- arm_a / arm_b must be copied from this list of arm names: {arms}
- p_value_type: "noninferiority" only if the paper says that p-value is for non-inferiority, otherwise "superiority".
""" + RULES

BASELINE_PROMPT = """Extract the BASELINE characteristics table (demographics, comorbidities, labs, medications, disease history).
- If the baseline table is not in the paper, set available_in_paper=false and say where it is in location_note.
- arm must be copied from this list: {arms}   or "All" for the whole population.
""" + RULES

SAFETY_PROMPT = """Extract adverse events and deaths per arm, as printed.
- arm must be copied from this list: {arms}   or "All" for the whole population.
""" + RULES


def pages_text(pdf_bytes: bytes) -> list[str]:
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        return [p.get_text() for p in pdf]


def _postprocess(study: StudyUI) -> StudyUI:
    for o in study.outcomes:
        if o.p_value_type is None:
            o.p_value_type = "superiority"
    return study


def _safe_part(schema, system, text) -> dict:
    """A failing side call must not lose the study: return {} and let the core survive."""
    try:
        return llm.structured_dict(schema, system, text)
    except Exception:
        return {}


def extract_study(pdf_bytes: bytes) -> StudyUI:
    pages = pages_text(pdf_bytes)
    text = "\n\n".join(f"[PAGE {i}]\n{t}" for i, t in enumerate(pages, start=1))
    if len(text.strip()) < 500:
        raise ValueError("No readable text in this PDF (scanned PDF? OCR is not supported yet).")
    text = text[:MAX_CHARS]

    core = llm.structured_dict(Core, CORE_PROMPT, text)            # 1) small call: need the arm names first
    if not core.get("arms"):
        raise ValueError("Could not find any treatment arms or study groups in this PDF.")
    arms = ", ".join(f'"{a["name"]}"' for a in core["arms"] if a.get("name"))

    with ThreadPoolExecutor(max_workers=3) as ex:                  # 2) three calls at the same time
        f_out = ex.submit(_safe_part, OutcomesPart, OUTCOMES_PROMPT.format(arms=arms), text)
        f_base = ex.submit(_safe_part, BaselinePart, BASELINE_PROMPT.format(arms=arms), text)
        f_safe = ex.submit(_safe_part, SafetyPart, SAFETY_PROMPT.format(arms=arms), text)
        out, base, safe = f_out.result(), f_base.result(), f_safe.result()

    data = dict(core)
    data.update(out)
    data.update(base)
    data.update(safe)
    if not (out or base or safe):
        data["not_reported"] = list(data.get("not_reported") or []) + ["detailed tables could not be read from this PDF"]
    return _postprocess(validate_lenient(data))
