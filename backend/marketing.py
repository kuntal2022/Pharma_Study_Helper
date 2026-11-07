"""marketing.py - Sales & Marketing outputs built ONLY from the user's saved studies.

kinds:  brochure | slides | qa | mr_short | mr_long
Flow:   facts (exact numbers, built in code from the database)  ->  LLM writes the copy from those facts
        ->  code checks every number in the copy against the facts (unverified numbers are returned as warnings)
        ->  export to .docx (brochure) / .pptx (slides) with python-docx / python-pptx.

Everything is promotional material about published studies: the exported files carry a review note
(medical advisory / regulatory review needed before use).
"""
import io
import re
from typing import List, Literal, Optional

from pydantic import BaseModel

import llm
from models import StudyUI, display_label
from verdict import get_verdict

REVIEW_NOTE = ("Draft for internal use. Must be reviewed and approved by Medical Advisory / Regulatory "
               "before it is shared with any doctor or used in the field.")
KINDS = ("brochure", "slides", "qa", "mr_short", "mr_long")


# ----------------------------------------------------------------------------- facts (code, exact)
def _p(o) -> str:
    return o.p_value_text or (f"{o.p_value}" if o.p_value is not None else "n/a")


def study_facts(label: str, st: StudyUI) -> str:
    margin = st.statistics.noninferiority_margin
    d = st.design
    L = [f"=== STUDY [{label}] ===",
         f"Title: {d.study_name}. Year: {d.publication_year or 'n/a'}. Journal: {d.journal or 'n/a'}. Design: {d.design_type or 'n/a'}. "
         f"Median follow-up (years): {d.median_follow_up_years if d.median_follow_up_years is not None else 'n/a'}.",
         f"Population: {st.population.summary or 'n/a'}.",
         "Arms: " + "; ".join(f"{a.name} [{a.role}] n={a.n}" + (f", dose {a.dose}" if a.dose else "") for a in st.arms) + "."]
    for o in st.outcomes:
        v = get_verdict(o, margin)
        bits = [f"{o.outcome} ({o.outcome_type}, {o.outcome_group}{', PRIMARY' if o.is_primary else ''}; {o.arm_a} vs {o.arm_b})"]
        if o.events_a is not None:
            bits.append(f"events {o.events_a} vs {o.events_b}")
        if o.pct_a is not None:
            bits.append(f"{o.pct_a}% vs {o.pct_b}%")
        if o.effect is not None:
            bits.append(f"{o.effect_measure} {o.effect} (CI {o.ci_lower}-{o.ci_upper})")
        bits.append(f"p {_p(o)}" + (f" [{o.p_value_type}]" if o.p_value_type else ""))
        if v:
            bits.append(f"verdict: {v['label']}")
        L.append("Outcome: " + "; ".join(bits))
    for c in st.continuous_outcomes:
        L.append(f"Continuous: {c.name} ({c.timepoint or 'n/a'}, {c.unit or ''}): {c.arm_a} {c.value_a} vs {c.arm_b} {c.value_b}; "
                 f"difference {c.difference} (CI {c.ci_lower}-{c.ci_upper}); p {c.p_value_text or c.p_value}")
    for a in st.adverse_events[:14]:
        L.append(f"Safety: {a.event}{' (serious)' if a.is_serious else ''} - {a.arm}: n={a.patients_n}, {a.patients_pct}%")
    if st.conclusions.authors_conclusion:
        L.append("Authors' conclusion: " + st.conclusions.authors_conclusion)
    if st.conclusions.limitations:
        L.append("Limitations: " + "; ".join(st.conclusions.limitations[:4]))
    return "\n".join(L)


def build_facts(items: list[tuple[str, StudyUI]]) -> str:
    return "\n\n".join(study_facts(l, s) for l, s in items)


# ----------------------------------------------------------------------------- output schemas
class Point(BaseModel):
    headline: str
    evidence: str          # exact numbers from FACTS, with study label
    source: str            # study label


class BrochureSection(BaseModel):
    heading: str
    bullets: List[str]


class Brochure(BaseModel):
    title: str
    tagline: str
    sections: List[BrochureSection]
    key_numbers: List[str]


class KPI(BaseModel):
    value: str            # the number as in FACTS, e.g. "HR 0.98" or "14,671"
    label: str            # what it is, short
    tone: Literal["good", "neutral", "warn"] = "neutral"


class Slide(BaseModel):
    title: str
    layout: Literal["title", "flow", "kpi", "cards", "bullets"] = "bullets"
    bullets: List[str] = []
    steps: List[str] = []         # layout=flow: 3-5 short steps (e.g. Population > Randomised > Follow-up > Primary endpoint)
    kpis: List[KPI] = []          # layout=kpi: 3-4 big numbers
    notes: str = ""


class Deck(BaseModel):
    slides: List[Slide]


class QAItem(BaseModel):
    question: str
    answer: str


class QASet(BaseModel):
    items: List[QAItem]


class McShort(BaseModel):
    opening_line: str
    points: List[Point]


class McLong(BaseModel):
    opening_line: str
    usp_points: List[Point]
    drug_points: List[Point]          # why the (mother) medicine is best - only on this medicine
    comparison_points: List[Point]    # vs comparator / similar medicines, from the studies given
    comparison_note: str = ""


SCHEMAS = {"brochure": Brochure, "slides": Deck, "qa": QASet, "mr_short": McShort, "mr_long": McLong}

RULES = """You write promotional material for doctors and medical representatives about PUBLISHED clinical studies.
STRICT RULES:
- Use ONLY the FACTS given. Never add a number, drug property, indication or claim that is not in FACTS.
- Quote numbers exactly as in FACTS and name the study label next to them, e.g. "HR 0.98 (95% CI 0.89-1.08) [TECOS]".
- A result with a confidence interval that includes 1, or a p-value above 0.05, is NOT a benefit: say "no significant difference" / "non-inferior",
  never "better" or "reduced". Non-inferiority is not superiority.
- No off-label claims, no "best", "safest", "cures", no guarantees. Balanced, factual, doctor-friendly language. Include safety honestly.
- If two studies use different comparators or populations, say it is an indirect comparison from different trials.
- Keep each bullet short (max ~25 words)."""

TASKS = {
    "brochure": "Write a doctor-facing brochure: title, one-line tagline, 4-5 sections (e.g. Study at a glance, Efficacy, Cardiovascular/other key outcomes, Safety, Takeaway), and 4-6 key_numbers lines (exact, with study label).",
    "slides": "Write a visual 6-slide deck. Use these layouts: slide1 layout=title (title + 1-2 bullets as subtitle); slide2 layout=flow (study design as 4 steps, e.g. Population > Randomised arms > Follow-up > Primary endpoint, each step a short phrase with its fact); slide3 layout=kpi (3-4 big efficacy numbers: value + label, tone good only for a significant benefit or shown non-inferiority); slide4 layout=cards (3 bullets = 3 key findings, each short); slide5 layout=kpi (safety numbers) ; slide6 layout=bullets (takeaway + 'Indirect comparison' caution if several studies). Add 1-2 lines of speaker notes per slide.",
    "qa": "Write 8 likely questions a doctor would ask about these studies and short factual answers (2-3 sentences, with exact numbers and study label). Include at least 2 on safety and 1 on limitations.",
    "mr_short": "Short call (2 minutes) for a medical representative: one opening_line and exactly 5 points: top 5 USPs of the studied medicine and why it is a strong choice, each with exact evidence.",
    "mr_long": "Long call for a medical representative: one opening_line; usp_points (5-6 detailed points); drug_points (2-3 points about this medicine only, why it is a good choice); comparison_points (2-3 points comparing with the comparator in the studies or with similar medicines covered by the given studies; if the data is from different trials, say so in each point); comparison_note (one sentence on limits of the comparison).",
}


def generate(kind: str, items: list[tuple[str, StudyUI]]) -> dict:
    if kind not in KINDS:
        raise ValueError("Unknown kind")
    facts = build_facts(items)
    out = llm.structured_obj(SCHEMAS[kind], RULES, f"FACTS:\n{facts}\n\nTASK: {TASKS[kind]}")
    data = out.model_dump() if hasattr(out, "model_dump") else dict(out)
    return {"kind": kind, "content": data, "warnings": unverified_numbers(data, facts),
            "studies": [l for l, _ in items], "note": REVIEW_NOTE}


# ----------------------------------------------------------------------------- number check (code)
_NUM = re.compile(r"(?<![\w.])\d[\d,]*\.?\d*")


def _nums(text: str) -> set[str]:
    return {m.replace(",", "").rstrip(".") for m in _NUM.findall(text)}


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def unverified_numbers(content: dict, facts: str) -> list[str]:
    """Numbers in the generated copy that do not appear anywhere in FACTS (small counts like '5 points' ignored)."""
    ok = _nums(facts) | {"95", "99"}      # "95% CI" wording is common
    bad = []
    for s in _strings(content):
        for n in _nums(s):
            if n not in ok and not (n.isdigit() and int(n) <= 12):
                bad.append(n)
    return sorted(set(bad))


# ----------------------------------------------------------------------------- export
def to_docx(content: dict, studies: list[str]) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor
    b = Brochure.model_validate(content)
    doc = Document()
    doc.add_heading(b.title, 0)
    p = doc.add_paragraph(b.tagline)
    p.runs[0].italic = True
    note = doc.add_paragraph()
    r = note.add_run("Review note: " + REVIEW_NOTE)
    r.bold = True
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(0xB4, 0x23, 0x18)
    for s in b.sections:
        doc.add_heading(s.heading, 1)
        for t in s.bullets:
            doc.add_paragraph(t, style="List Bullet")
    if b.key_numbers:
        doc.add_heading("Key numbers", 1)
        for t in b.key_numbers:
            doc.add_paragraph(t, style="List Bullet")
    foot = doc.add_paragraph("Source studies: " + ", ".join(studies) + ". " + REVIEW_NOTE)
    foot.runs[0].font.size = Pt(8)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def to_pptx(content: dict, studies: list[str]) -> bytes:
    """Designed deck: native shapes (title band, flow chevrons, KPI cards, finding cards) - no pictures, fully editable."""
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt
    deck = Deck.model_validate(content)
    C = lambda h: RGBColor.from_string(h)
    NAVY, INDIGO, BG, INK, GREY = "0B2A5B", "4F46E5", "F5F6FF", "1F2340", "6B7280"
    TONE = {"good": ("16A34A", "E8F8EE"), "neutral": ("4F46E5", "EEF0FF"), "warn": ("F59E0B", "FFF4DC")}
    CARD = [("4F46E5", "EEF0FF"), ("0EA5A4", "E3F7F6"), ("F97316", "FFEFE3"), ("EC4899", "FDE7F2")]
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    W, H = prs.slide_width, prs.slide_height

    def box(sl, shape, x, y, w, h, fill, line=None):
        sp = sl.shapes.add_shape(shape, x, y, w, h)
        sp.fill.solid(); sp.fill.fore_color.rgb = C(fill)
        if line: sp.line.color.rgb = C(line); sp.line.width = Pt(1.5)
        else: sp.line.fill.background()
        sp.shadow.inherit = False
        return sp

    def text(sp_or_sl, x=None, y=None, w=None, h=None, t="", size=18, bold=False, color=INK, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
        tf = sp_or_sl.text_frame if x is None else sp_or_sl.shapes.add_textbox(x, y, w, h).text_frame
        tf.word_wrap = True; tf.vertical_anchor = anchor
        tf.margin_left = tf.margin_right = Inches(0.12)
        p = tf.paragraphs[0]; p.text = t; p.alignment = align
        p.font.size = Pt(size); p.font.bold = bold; p.font.color.rgb = C(color)
        return tf

    for i, s in enumerate(deck.slides):
        sl = prs.slides.add_slide(prs.slide_layouts[6])
        sl.background.fill.solid(); sl.background.fill.fore_color.rgb = C(BG)
        title_slide = s.layout == "title" or i == 0
        if title_slide:
            box(sl, MSO_SHAPE.RECTANGLE, 0, 0, W, H, NAVY)
            box(sl, MSO_SHAPE.RECTANGLE, 0, Inches(5.2), W, Inches(0.12), "EC4899")
            text(sl, Inches(0.9), Inches(2.0), Inches(11.5), Inches(1.8), s.title, 44, True, "FFFFFF", anchor=MSO_ANCHOR.BOTTOM)
            text(sl, Inches(0.9), Inches(3.9), Inches(11.5), Inches(1.2), " | ".join(s.bullets[:2]), 20, False, "C7D2FE")
        else:
            box(sl, MSO_SHAPE.RECTANGLE, 0, 0, W, Inches(1.15), NAVY)
            box(sl, MSO_SHAPE.RECTANGLE, 0, Inches(1.15), W, Inches(0.07), "EC4899")
            text(sl, Inches(0.5), Inches(0.15), Inches(12.3), Inches(0.9), s.title, 30, True, "FFFFFF", anchor=MSO_ANCHOR.MIDDLE)
            if s.layout == "flow" and s.steps:
                n = min(len(s.steps), 5); gap = Inches(0.15); cw = int((W - Inches(1.0) - gap * (n - 1)) / n)
                for j, st in enumerate(s.steps[:n]):
                    acc, soft = CARD[j % 4]
                    x = Inches(0.5) + j * (cw + gap)
                    ch = box(sl, MSO_SHAPE.PENTAGON if j < n - 1 else MSO_SHAPE.ROUNDED_RECTANGLE, x, Inches(2.0), cw, Inches(1.0), acc)
                    text(ch, t=f"Step {j + 1}", size=16, bold=True, color="FFFFFF", align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
                    cd = box(sl, MSO_SHAPE.ROUNDED_RECTANGLE, x, Inches(3.25), cw, Inches(2.6), soft, acc)
                    text(cd, t=st, size=17, color=INK, anchor=MSO_ANCHOR.MIDDLE, align=PP_ALIGN.CENTER)
                if s.bullets:
                    text(sl, Inches(0.6), Inches(6.0), Inches(12.1), Inches(0.7), " | ".join(s.bullets[:2]), 15, False, GREY)
            elif s.layout == "kpi" and s.kpis:
                n = min(len(s.kpis), 4); gap = Inches(0.25); cw = int((W - Inches(1.0) - gap * (n - 1)) / n)
                for j, k in enumerate(s.kpis[:n]):
                    acc, soft = TONE.get(k.tone, TONE["neutral"])
                    x = Inches(0.5) + j * (cw + gap)
                    box(sl, MSO_SHAPE.ROUNDED_RECTANGLE, x, Inches(1.8), cw, Inches(3.9), soft, acc)
                    box(sl, MSO_SHAPE.RECTANGLE, x + Inches(0.3), Inches(1.8), cw - Inches(0.6), Inches(0.12), acc)
                    text(sl, x, Inches(2.3), cw, Inches(1.5), k.value, 40 if len(k.value) < 12 else 28, True, acc, PP_ALIGN.CENTER, MSO_ANCHOR.MIDDLE)
                    text(sl, x, Inches(3.9), cw, Inches(1.6), k.label, 16, False, INK, PP_ALIGN.CENTER)
                if s.bullets:
                    text(sl, Inches(0.6), Inches(5.9), Inches(12.1), Inches(0.9), "   |   ".join(s.bullets[:3]), 15, False, GREY)
            elif s.layout == "cards" and s.bullets:
                n = min(len(s.bullets), 4); gap = Inches(0.25); cw = int((W - Inches(1.0) - gap * (n - 1)) / n)
                for j, b in enumerate(s.bullets[:n]):
                    acc, soft = CARD[j % 4]
                    x = Inches(0.5) + j * (cw + gap)
                    box(sl, MSO_SHAPE.ROUNDED_RECTANGLE, x, Inches(1.8), cw, Inches(4.6), soft, acc)
                    num = box(sl, MSO_SHAPE.OVAL, x + Inches(0.25), Inches(2.0), Inches(0.8), Inches(0.8), acc)
                    text(num, t=str(j + 1), size=24, bold=True, color="FFFFFF", align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
                    text(sl, x + Inches(0.1), Inches(3.0), cw - Inches(0.2), Inches(3.2), b, 18, False, INK)
            else:
                for j, b in enumerate(s.bullets[:6]):
                    acc, soft = CARD[j % 4]
                    y = Inches(1.65) + j * Inches(0.85)
                    box(sl, MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.6), y, Inches(12.1), Inches(0.72), soft)
                    box(sl, MSO_SHAPE.RECTANGLE, Inches(0.6), y, Inches(0.14), Inches(0.72), acc)
                    text(sl, Inches(0.9), y, Inches(11.7), Inches(0.72), b, 18, False, INK, anchor=MSO_ANCHOR.MIDDLE)
        foot = "Sources: " + ", ".join(studies) + "  |  " + REVIEW_NOTE
        text(sl, Inches(0.4), Inches(6.95), Inches(12.5), Inches(0.45), foot, 9, False, "C7D2FE" if title_slide else GREY)
        if s.notes:
            sl.notes_slide.notes_text_frame.text = s.notes
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
