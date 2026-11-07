"""
Pharma Study Analyser - extraction schema (v2).

Design rules
------------
1. Every leaf number has an Optional value + source_page. Missing in the paper -> None
   (never guessed). Things that live only in a Supplementary Appendix are listed in
   `not_reported` / `supplement_refs`, so the UI can say "not in this PDF".
2. Arms are a LIST (placebo-only, 1 drug, 3 drugs ... all work). Every other record
   refers to an arm by NAME; validators check the names exist ("All" = whole population).
3. The schema is split in sections so that extraction can run as several SMALL LLM calls
   (design / baseline / outcomes / safety / statistics) and be merged into StudyUI.
4. The verdict (superior / noninferior / ...) is NOT extracted by the LLM. It is computed
   by rule code from the extracted outcome rows (see verdict.py, later).
"""
from __future__ import annotations

from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, Field, computed_field, model_validator

ALL_ARMS = {"all", "overall", "total"}

Pct = Annotated[float, Field(ge=0, le=100)]
Prob = Annotated[float, Field(ge=0, le=1)]


# --------------------------------------------------------------------------- #
# 1. Study design / meta
# --------------------------------------------------------------------------- #
class StudyDesign(BaseModel):
    study_name: str = Field(..., description="Full title, e.g. Trial Evaluating Cardiovascular Outcomes with Sitagliptin")
    acronym: Optional[str] = Field(None, examples=["TECOS"])
    journal: Optional[str] = None
    publication_year: Optional[int] = None
    doi: Optional[str] = None
    registry_id: Optional[str] = Field(None, description="e.g. NCT00790205")
    funder: Optional[str] = None
    design_type: Optional[str] = Field(None, description="e.g. randomized, double-blind, placebo-controlled, event-driven")
    randomization_ratio: Optional[str] = Field(None, examples=["1:1"])
    blinding: Optional[Literal["open_label", "single_blind", "double_blind", "other"]] = None
    comparator_type: Optional[Literal["placebo", "active", "usual_care", "none", "other"]] = None
    n_sites: Optional[int] = Field(None, ge=0)
    n_countries: Optional[int] = Field(None, ge=0)
    enrollment_start: Optional[str] = Field(None, description="As printed, e.g. December 2008")
    enrollment_end: Optional[str] = None
    study_closed: Optional[str] = None
    median_follow_up_years: Optional[float] = Field(None, ge=0, le=100)
    follow_up_iqr: Optional[str] = Field(None, examples=["2.3 to 3.8"])
    max_follow_up_years: Optional[float] = Field(None, ge=0, le=100)
    source_page: Optional[int] = None


class Population(BaseModel):
    summary: Optional[str] = Field(None, description="One line: who was studied")
    age_min: Optional[int] = Field(None, ge=0)
    hba1c_range: Optional[str] = Field(None, examples=["6.5 to 8.0%"])
    inclusion_criteria: List[str] = []
    exclusion_criteria: List[str] = []
    source_page: Optional[int] = None


# --------------------------------------------------------------------------- #
# 2. Arms + patient disposition
# --------------------------------------------------------------------------- #
class Arm(BaseModel):
    name: str = Field(..., examples=["Placebo", "Sitagliptin"])
    role: Literal["main_drug", "comparator", "placebo"]
    dose: Optional[str] = Field(None, examples=["100 mg daily (50 mg if eGFR 30-<50)"])
    n_randomized: Optional[int] = Field(None, ge=0)
    n_itt: Optional[int] = Field(None, ge=0, description="Patients in the intention-to-treat analysis")
    n_per_protocol: Optional[int] = Field(None, ge=0)
    completed_n: Optional[int] = Field(None, ge=0)
    completed_pct: Optional[Pct] = None
    discontinued_drug_n: Optional[int] = Field(None, ge=0)
    discontinued_drug_pct: Optional[Pct] = None
    lost_to_followup_n: Optional[int] = Field(None, ge=0)
    withdrew_consent_n: Optional[int] = Field(None, ge=0)
    source_page: Optional[int] = None

    @property
    def n(self) -> Optional[int]:
        """Best available N: ITT, else randomized."""
        return self.n_itt if self.n_itt is not None else self.n_randomized


class Disposition(BaseModel):
    n_randomized_total: Optional[int] = Field(None, ge=0)
    n_excluded_from_all_analyses: Optional[int] = Field(None, ge=0)
    exclusion_reasons: List[str] = []
    vital_status_obtained_pct: Optional[Pct] = None
    source_page: Optional[int] = None


# --------------------------------------------------------------------------- #
# 3. Baseline characteristics (comorbidities, demographics, labs, medications)
# --------------------------------------------------------------------------- #
class BaselineItem(BaseModel):
    name: str = Field(..., description="Condition/measure present at BASELINE (e.g. hypertension, prior MI, age, HbA1c, metformin use). NOT events during follow-up.")
    category: Literal["demographic", "comorbidity", "lab_vital", "medication", "diabetes_history", "other"]
    arm: str = Field(..., description='Arm name exactly as in arms, or "All" for the whole population')
    value_type: Literal["count_pct", "percent", "mean_sd", "median_iqr", "text"] = "count_pct"
    count: Optional[int] = Field(None, ge=0)
    percent: Optional[Pct] = None
    mean: Optional[float] = None
    sd: Optional[float] = None
    median: Optional[float] = None
    iqr: Optional[str] = None
    unit: Optional[str] = None
    text: Optional[str] = Field(None, description="Use only when value is not numeric")
    source_page: Optional[int] = None


class BaselineSection(BaseModel):
    available_in_paper: bool = Field(
        ..., description="True only if a baseline-characteristics table with numbers is inside this document"
    )
    location_note: Optional[str] = Field(None, examples=["Supplementary Appendix 1, Table S1"])
    items: List[BaselineItem] = []


# --------------------------------------------------------------------------- #
# 4. Outcomes (efficacy + safety), one row = one comparison on one outcome
# --------------------------------------------------------------------------- #
class OutcomeResult(BaseModel):
    outcome: str = Field(..., examples=["Primary composite (CV death, MI, stroke, unstable angina)"])
    outcome_group: Literal["primary", "secondary_composite", "secondary", "component", "safety", "exploratory"]
    outcome_type: Literal["efficacy", "safety"] = Field(
        "efficacy",
        description="safety = adverse events (pancreatitis, cancer, hypoglycemia, HF hospitalization). efficacy = trial efficacy outcomes incl. death.",
    )
    is_primary: bool = False
    analysis_set: Optional[Literal["ITT", "per_protocol", "other"]] = None
    arm_a: str = Field(..., description="Main drug arm name")
    arm_b: str = Field(..., description="Placebo / comparator arm name")

    events_a: Optional[int] = Field(None, ge=0)
    events_b: Optional[int] = Field(None, ge=0)
    pct_a: Optional[Pct] = Field(None, description="Percent of arm_a with event, as printed")
    pct_b: Optional[Pct] = None
    rate_a: Optional[float] = Field(None, ge=0, description="Events per 100 person-years, arm_a")
    rate_b: Optional[float] = Field(None, ge=0)

    effect_measure: Optional[Literal["HR", "OR", "RR", "MD", "other"]] = None
    effect: Optional[float] = None
    ci_lower: Optional[float] = None
    ci_upper: Optional[float] = None
    ci_level: Optional[float] = Field(95.0, description="Confidence level in percent")

    p_value: Optional[Prob] = Field(None, description="Plain numeric p only (0.65). None if printed as '<0.001'")
    p_value_text: Optional[str] = Field(None, description='p exactly as printed, e.g. "<0.001"')
    p_value_type: Optional[Literal["superiority", "noninferiority", "interaction", "other"]] = None
    adjusted_for: Optional[str] = Field(None, description="Footnote adjustment, e.g. history of heart failure at baseline")
    source_page: Optional[int] = None

    @model_validator(mode="after")
    def _sane_numbers(self):
        if None not in (self.ci_lower, self.ci_upper) and self.ci_lower > self.ci_upper:
            raise ValueError(f"{self.outcome}: ci_lower > ci_upper")
        if None not in (self.effect, self.ci_lower, self.ci_upper):
            tol = 0.02
            if not (self.ci_lower - tol <= self.effect <= self.ci_upper + tol):
                raise ValueError(f"{self.outcome}: effect {self.effect} outside CI [{self.ci_lower}, {self.ci_upper}]")
        return self


class ContinuousOutcome(BaseModel):
    """HbA1c change, eGFR change, weight, BP ... measured as a mean/difference."""
    name: str = Field(..., examples=["HbA1c, LS mean difference", "eGFR change at 48 months"])
    timepoint: Optional[str] = Field(None, examples=["overall", "4 months", "48 months"])
    unit: Optional[str] = Field(None, examples=["percentage points", "ml/min/1.73 m2"])
    arm_a: Optional[str] = None
    arm_b: Optional[str] = None
    value_a: Optional[float] = None
    value_b: Optional[float] = None
    sd_a: Optional[float] = None
    sd_b: Optional[float] = None
    difference: Optional[float] = None
    ci_lower: Optional[float] = None
    ci_upper: Optional[float] = None
    p_value: Optional[Prob] = None
    p_value_text: Optional[str] = None
    source_page: Optional[int] = None


class SubgroupResult(BaseModel):
    outcome: str
    subgroup: str = Field(..., examples=["BMI", "history of heart failure"])
    level: Optional[str] = Field(None, examples=["<30", ">=30"])
    effect_measure: Optional[Literal["HR", "OR", "RR", "other"]] = "HR"
    effect: Optional[float] = None
    ci_lower: Optional[float] = None
    ci_upper: Optional[float] = None
    p_interaction: Optional[Prob] = None
    source_page: Optional[int] = None


# --------------------------------------------------------------------------- #
# 5. Safety / adverse events
# --------------------------------------------------------------------------- #
class AdverseEvent(BaseModel):
    event: str = Field(..., examples=["Gastrointestinal disorder", "Serious infections"])
    arm: str
    is_serious: bool = False
    patients_n: Optional[int] = Field(None, ge=0)
    patients_pct: Optional[Pct] = None
    events_n: Optional[int] = Field(None, ge=0)
    source_page: Optional[int] = None


class DeathSummary(BaseModel):
    arm: str
    cause: str = Field(..., examples=["non-cardiovascular", "infection"])
    pct: Optional[Pct] = None
    n: Optional[int] = Field(None, ge=0)
    source_page: Optional[int] = None


# --------------------------------------------------------------------------- #
# 6. Statistics + conclusions
# --------------------------------------------------------------------------- #
class StatisticalPlan(BaseModel):
    primary_hypothesis: Optional[Literal["noninferiority", "superiority", "both", "other"]] = None
    noninferiority_margin: Optional[float] = Field(None, description="e.g. 1.3 (upper CI bound for HR)")
    margin_scale: Optional[Literal["HR", "RR", "OR", "MD", "other"]] = "HR"
    primary_analysis_set: Optional[Literal["ITT", "per_protocol", "other"]] = None
    supporting_analysis_set: Optional[Literal["ITT", "per_protocol", "other"]] = None
    model: Optional[str] = Field(None, examples=["Cox proportional hazards, stratified by region"])
    testing_sequence: Optional[str] = Field(None, description="Order of hypotheses tested, if sequential")
    target_events: Optional[int] = Field(None, ge=0)
    power_pct: Optional[Pct] = None
    sample_size_note: Optional[str] = None
    software: Optional[str] = None
    source_page: Optional[int] = None


class Conclusions(BaseModel):
    authors_conclusion: Optional[str] = Field(None, description="Main conclusion as stated by the authors")
    limitations: List[str] = []
    strengths: List[str] = []
    key_message_one_line: Optional[str] = Field(None, description="Plain one-line takeaway using only facts in the paper")
    source_page: Optional[int] = None


# --------------------------------------------------------------------------- #
# 7. The whole study object (what goes to SQL / UI)
# --------------------------------------------------------------------------- #
class StudyUI(BaseModel):
    design: StudyDesign
    population: Population = Population()
    arms: List[Arm] = Field(..., min_length=1)
    disposition: Disposition = Disposition()
    baseline: BaselineSection = BaselineSection(available_in_paper=False)
    outcomes: List[OutcomeResult] = []
    continuous_outcomes: List[ContinuousOutcome] = []
    subgroups: List[SubgroupResult] = []
    adverse_events: List[AdverseEvent] = []
    deaths: List[DeathSummary] = []
    statistics: StatisticalPlan = StatisticalPlan()
    conclusions: Conclusions = Conclusions()

    supplement_refs: List[str] = Field(
        [], description="Data the paper says is in a supplement, e.g. 'Table S1 baseline characteristics'"
    )
    not_reported: List[str] = Field(
        [], description="Important items that are NOT in this document (so UI shows 'Not reported')"
    )

    # ---- derived (computed in code, never asked from the LLM) -------------- #
    @computed_field
    @property
    def patient_count(self) -> Optional[int]:
        ns = [a.n for a in self.arms]
        return None if any(n is None for n in ns) else sum(ns)

    @computed_field
    @property
    def primary_outcomes(self) -> List[str]:
        return sorted({o.outcome for o in self.outcomes if o.is_primary})

    # ---- make arm names consistent (any paper, any wording) --------------- #
    @model_validator(mode="before")
    @classmethod
    def _canonical_arm_names(cls, data):
        """LLMs often write 'Saxagliptin' in an outcome but 'Saxagliptin 5 mg' in arms.
        Map every arm-name field to the exact name in arms when the match is clear."""
        if not isinstance(data, dict) or not data.get("arms"):
            return data
        arms = [a for a in data["arms"] if isinstance(a, dict) and a.get("name")]
        names = [a["name"] for a in arms]

        def canon(n):
            if not isinstance(n, str) or n in names or n.lower() in ALL_ARMS:
                return n
            low = n.strip().lower()
            for x in names:                                   # case-insensitive exact
                if x.lower() == low:
                    return x
            hits = [x for x in names if low in x.lower() or x.lower() in low]   # 'Saxagliptin' <-> 'Saxagliptin 5 mg'
            if len(hits) == 1:
                return hits[0]
            if "placebo" in low:                              # 'placebo' / 'matching placebo'
                pl = [a["name"] for a in arms if a.get("role") == "placebo"]
                if len(pl) == 1:
                    return pl[0]
            first = low.split()[0] if low.split() else low    # same first word (drug name)
            hits = [x for x in names if x.lower().split()[:1] == [first]]
            return hits[0] if len(hits) == 1 else n

        dropped = 0

        def good(n, allow_all=True, allow_none=False):
            if n is None:
                return allow_none
            return n in names or (allow_all and str(n).lower() in ALL_ARMS)

        def lim(arm_name, aset):
            a = next((x for x in arms if x["name"] == arm_name), None)
            if not a:
                return None
            if aset == "per_protocol" and a.get("n_per_protocol"):
                return a["n_per_protocol"]
            return a.get("n_itt") if a.get("n_itt") is not None else a.get("n_randomized")

        def fix_rows(rows, fields, allow_all, allow_none):
            nonlocal dropped
            keep = []
            for r in rows or []:
                if not isinstance(r, dict):
                    continue
                for f in fields:
                    if f in r:
                        r[f] = canon(r[f])
                if all(good(r.get(f), allow_all, allow_none) for f in fields):
                    keep.append(r)
                else:
                    dropped += 1                      # arm name could not be matched: drop the row, keep the study
            return keep

        out = fix_rows(data.get("outcomes"), ("arm_a", "arm_b"), False, False)
        for r in out:                                 # events can never exceed patients: blank the number, keep the row
            for arm_f, ev_f in (("arm_a", "events_a"), ("arm_b", "events_b")):
                lm, ev = lim(r[arm_f], r.get("analysis_set")), r.get(ev_f)
                if lm is not None and isinstance(ev, (int, float)) and ev > lm:
                    r[ev_f] = None
        data["outcomes"] = out
        data["continuous_outcomes"] = fix_rows(data.get("continuous_outcomes"), ("arm_a", "arm_b"), True, True)
        data["adverse_events"] = fix_rows(data.get("adverse_events"), ("arm",), True, False)
        data["deaths"] = fix_rows(data.get("deaths"), ("arm",), True, False)
        bl = data.get("baseline")
        if isinstance(bl, dict):
            bl["items"] = fix_rows(bl.get("items"), ("arm",), True, False)
        if dropped:
            nr = list(data.get("not_reported") or [])
            nr.append(f"{dropped} extracted rows were left out because their arm name did not match the study arms")
            data["not_reported"] = nr
        return data

    # ---- consistency checks ------------------------------------------------ #
    @model_validator(mode="after")
    def _arm_names_and_counts(self):
        names = {a.name for a in self.arms}

        def ok(n: str) -> bool:
            return n in names or n.lower() in ALL_ARMS

        for o in self.outcomes:
            if o.arm_a not in names or o.arm_b not in names:
                raise ValueError(f"outcome arm not in arms: {o.arm_a} vs {o.arm_b} ({o.outcome})")
            # events can never exceed the patients in that analysis set
            for arm_name, ev in ((o.arm_a, o.events_a), (o.arm_b, o.events_b)):
                if ev is None:
                    continue
                arm = next(a for a in self.arms if a.name == arm_name)
                limit = arm.n_per_protocol if o.analysis_set == "per_protocol" and arm.n_per_protocol else arm.n
                if limit is not None and ev > limit:
                    raise ValueError(f"{o.outcome}: events {ev} > patients {limit} in {arm_name}")
        for b in self.baseline.items:
            if not ok(b.arm):
                raise ValueError(f"baseline arm not in arms: {b.arm} ({b.name})")
        for ae in self.adverse_events:
            if not ok(ae.arm):
                raise ValueError(f"adverse-event arm not in arms: {ae.arm}")
        for d in self.deaths:
            if not ok(d.arm):
                raise ValueError(f"death-summary arm not in arms: {d.arm}")
        for c in self.continuous_outcomes:
            for nm in (c.arm_a, c.arm_b):
                if nm is not None and not ok(nm):
                    raise ValueError(f"continuous outcome arm not in arms: {nm}")
        return self


# --------------------------------------------------------------------------- #
# lenient loading: one bad row must never throw away the whole study
# --------------------------------------------------------------------------- #
_LIST_KEYS = ("outcomes", "continuous_outcomes", "subgroups", "adverse_events", "deaths")


def validate_lenient(data: dict) -> "StudyUI":
    """Validate; rows that fail (bad enum, out-of-range number ...) are dropped and noted, up to 6 rounds.
    Raises only when the core (design / arms) is unusable."""
    from pydantic import ValidationError
    data = dict(data)
    dropped = 0
    for _ in range(6):
        try:
            study = StudyUI.model_validate(data)
            if dropped:
                study.not_reported.append(f"{dropped} extracted rows failed validation and were left out")
            return study
        except ValidationError as e:
            bad = {}
            for err in e.errors():
                loc = err["loc"]
                if len(loc) >= 2 and loc[0] in _LIST_KEYS and isinstance(loc[1], int):
                    bad.setdefault(loc[0], set()).add(loc[1])
                elif len(loc) >= 3 and loc[0] == "baseline" and loc[1] == "items" and isinstance(loc[2], int):
                    bad.setdefault("baseline.items", set()).add(loc[2])
                else:
                    raise
            for key, idxs in bad.items():
                if key == "baseline.items":
                    bl = dict(data["baseline"]); rows = list(bl["items"])
                    data["baseline"] = bl
                    bl["items"] = [r for i, r in enumerate(rows) if i not in idxs]
                else:
                    data[key] = [r for i, r in enumerate(data[key]) if i not in idxs]
                dropped += len(idxs)
    return StudyUI.model_validate(data)


def display_label(design: "StudyDesign") -> str:
    """Short name for cards/headers: acronym, else the title before ':' (max ~48 chars)."""
    if design.acronym:
        return design.acronym
    t = (design.study_name or "Study").split(":")[0].strip()
    return t if len(t) <= 48 else t[:46].rstrip() + "…"
