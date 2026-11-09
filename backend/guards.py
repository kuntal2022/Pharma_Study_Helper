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

# Each entry is (label, pattern). A regex is just a search pattern: "\\d" = one digit, "{4}" = exactly 4 times,
# "[A-Z]" = one capital letter, "+" = one or more. When a pattern matches, the text is replaced by [LABEL].
_PII = (
    # Email: some characters, an "@", a domain, a dot, an ending.            e.g. "doc.name@gmail.com"
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # Aadhaar: 4 digits, a space, 4 digits, a space, 4 digits.               e.g. "1234 5678 9012"
    ("AADHAAR", re.compile(r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)")),
    # PAN card: 5 capital letters, 4 digits, 1 capital letter.               e.g. "ABCDE1234F"
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    # Indian mobile: optional +91, then 10 digits starting with 6, 7, 8 or 9.   e.g. "9876543210"
    ("PHONE", re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s-]?)?[6-9]\d{9}(?!\d)")),
    # International number: "+", country code, then 8 to 12 digits.           e.g. "+44 7911123456"
    ("PHONE", re.compile(r"(?<![\w.])\+\d{1,3}[\s-]?\d{8,12}(?!\d)")),
)


def mask_pii(text: str) -> Tuple[str, List[str]]:
    kinds = []
    for kind, rx in _PII:
        text, n = rx.subn(f"[{kind}]", text)
        if n:
            kinds.append(kind)
    return text, kinds


# Questions that ask for PERSONAL medical advice (a dose, a diagnosis, "what should I take").
# "|" inside (..|..) means "or"; ".*" means "anything in between"; re.I = ignore capital/small letters.
_PERSONAL = [re.compile(p, re.I) for p in (
    # "should I take / can I stop / may I start / could I skip ..."
    r"\b(should|can|may|could|must) i (take|stop|start|use|skip|increase|reduce|double|inject|switch)\b",
    # "my dose", "for my tablet", "my insulin", "my treatment"
    r"\b(my|for my) (dose|dosage|medicine|medication|tablet|tablets|insulin|prescription|treatment)\b",
    # "how much ... should I take", "how many ... can I use"
    r"\bhow (much|many)\b.*\b(should i|do i need to|can i) (take|use|inject)\b",
    # "prescribe me", "diagnose my ..."
    r"\b(prescribe|diagnose) (me|my)\b",
    # "do I have ...", "am I diabetic"
    r"\b(do|am) i (have|suffering|diabetic)\b",
    # "I have diabetes, which tablet should I take?" / "my father has ..., what medicine can he take?"
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


# A number: a digit, then more digits or commas, then an optional decimal part.   e.g. "7,020", "10.5", "0.86"
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
