"""guards.py - input and output guardrails for the study chat. No extra libraries, no extra LLM calls.

INPUT  (before anything is stored or sent to the LLM)
  - mask_pii(): emails, phone numbers, Aadhaar, PAN -> [EMAIL] [PHONE] [AADHAAR] [PAN]
    (names cannot be caught by rules; use Presidio later if you need that)
  - is_personal_advice(): "what dose should I take", "I have diabetes, which tablet..." -> refuse politely
  General questions (2*2, hello, what is MACE) are NOT blocked here; chat_graph routes them to a plain LLM answer.

OUTPUT (after the answer is generated from the study excerpts)
  - ungrounded_numbers(): every number in the answer must appear in the excerpts (the prompt already says
    "copy numbers exactly"). If one does not, the answer gets a warning line and is NOT cached.
"""
import re
from typing import List, Tuple

_PII = (
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("AADHAAR", re.compile(r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)")),
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("PHONE", re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s-]?)?[6-9]\d{9}(?!\d)")),
    ("PHONE", re.compile(r"(?<![\w.])\+\d{1,3}[\s-]?\d{8,12}(?!\d)")),
)


def mask_pii(text: str) -> Tuple[str, List[str]]:
    kinds = []
    for kind, rx in _PII:
        text, n = rx.subn(f"[{kind}]", text)
        if n:
            kinds.append(kind)
    return text, kinds


_PERSONAL = [re.compile(p, re.I) for p in (
    r"\b(should|can|may|could|must) i (take|stop|start|use|skip|increase|reduce|double|inject|switch)\b",
    r"\b(my|for my) (dose|dosage|medicine|medication|tablet|tablets|insulin|prescription|treatment)\b",
    r"\bhow (much|many)\b.*\b(should i|do i need to|can i) (take|use|inject)\b",
    r"\b(prescribe|diagnose) (me|my)\b",
    r"\b(do|am) i (have|suffering|diabetic)\b",
    r"\b(i have|i am suffering|i'm suffering|i was diagnosed|my (father|mother|wife|husband|son|daughter|child|baby))\b"
    r".*\b(which|what|should|can)\b.*\b(medicine|drug|tablet|dose|dosage|treatment|take)\b",
)]

REFUSAL_PERSONAL = ("I can't give personal medical advice such as a dose, a diagnosis or what you should take. "
                    "Please ask your doctor or pharmacist. I can explain what the study reports "
                    "(for example: results, doses tested, side effects), so ask me about the paper.")

UNVERIFIED_NOTE = ("Note: some numbers in this answer could not be matched to the paper's excerpts. "
                   "Please check them in the paper before relying on them.")


def is_personal_advice(q: str) -> bool:
    return any(rx.search(q) for rx in _PERSONAL)


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _clean(num: str) -> str:
    return num.replace(",", "").rstrip(".")


def ungrounded_numbers(answer: str, context: str, question: str = "") -> List[str]:
    """Numbers that appear in the answer but not in the excerpts (or the question). Page cites like [p.7] are ignored."""
    body = re.sub(r"\[[^\]]*\]", " ", answer)                       # drop [p.7] / [LABEL p.7]
    ctx = re.sub(r"(?<=\d),(?=\d)", "", context)
    qtext = re.sub(r"(?<=\d),(?=\d)", "", question)
    bad = []
    for raw in _NUM.findall(body):
        n = _clean(raw)
        if len(n) <= 1:                                              # "1", "3" (counts like "3 arms")
            continue
        pat = r"(?<![\d.])" + re.escape(n) + r"(?!\d)"
        if re.search(pat, ctx) or re.search(pat, qtext):
            continue
        if n not in bad:
            bad.append(n)
    return bad
