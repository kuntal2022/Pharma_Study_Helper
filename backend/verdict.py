"""verdict.py - rule-based verdict from effect + CI (no LLM, so it cannot hallucinate).

Rules (ratio measures: HR / OR / RR, where 1 = no difference):
  non-inferiority row : CI upper < margin  -> Non-inferior ; else Inconclusive
  superiority row     : CI upper < 1 -> lower risk ; CI lower > 1 -> higher risk ; else no significant difference
For safety outcomes the same numbers are read as: lower risk = good, higher risk = bad.
tone is used by the UI for colour: good / bad / neutral / warn
"""
from typing import Optional


def get_verdict(o, margin: Optional[float] = None) -> Optional[dict]:
    lo, hi, eff = o.ci_lower, o.ci_upper, o.effect
    if lo is None or hi is None or o.effect_measure not in ("HR", "OR", "RR"):
        return None
    arm = o.arm_a
    if o.p_value_type == "noninferiority":
        m = margin
        if m is None:
            return {"key": "inconclusive", "tone": "warn", "label": "Non-inferiority margin not stated",
                    "text": "The paper's non-inferiority margin was not extracted, so no verdict is given."}
        if hi < m:
            return {"key": "noninferior", "tone": "good", "label": "Non-inferior",
                    "text": f"Upper CI {hi} is below the margin {m}: {arm} is not worse than {o.arm_b}."}
        return {"key": "inconclusive", "tone": "warn", "label": "Non-inferiority not shown",
                "text": f"Upper CI {hi} is not below the margin {m}."}
    if hi < 1:
        return {"key": "lower", "tone": "good", "label": "Lower risk",
                "text": f"CI {lo}-{hi} is entirely below 1: fewer events with {arm}."}
    if lo > 1:
        return {"key": "higher", "tone": "bad", "label": "Higher risk",
                "text": f"CI {lo}-{hi} is entirely above 1: more events with {arm}."}
    return {"key": "nodiff", "tone": "neutral", "label": "No significant difference",
            "text": f"CI {lo}-{hi} includes 1: no clear difference between {arm} and {o.arm_b}."}


def overall_verdict(study) -> Optional[dict]:
    """One headline for the study, from the primary outcome rows."""
    margin = study.statistics.noninferiority_margin
    prim = [o for o in study.outcomes if o.is_primary]
    best = None
    for o in prim:
        v = get_verdict(o, margin)
        if not v:
            continue
        v = {**v, "analysis_set": o.analysis_set, "outcome": o.outcome}
        if v["key"] == "noninferior" or best is None:
            best = v
    if best is None:
        return None
    sup = [get_verdict(o, margin) for o in prim if o.p_value_type != "noninferiority"]
    sup = [v for v in sup if v]
    best["summary"] = best["label"] + (" · " + sup[0]["label"] if sup and best["key"] == "noninferior" else "")
    return best
