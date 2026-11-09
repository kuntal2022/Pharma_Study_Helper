"""
Pharma Study Analyser - database layer (Azure SQL, SQLite fallback for local dev).

Hybrid design:
  * studies          : one row per (user, PDF) + the full StudyUI as raw_json
  * arms / outcomes / baseline_items / adverse_events / continuous_outcomes / deaths
                     : the same data in normal columns, so compare / filter / charts
                       can be done with plain SQL.
Every study belongs to ONE user (owner_user_id). A user can never see another user's row.
"""
import os
import uuid
import datetime as dt
from urllib.parse import quote_plus
from typing import List, Optional, Tuple

from dotenv import load_dotenv
from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, MetaData, String, Table,
    UniqueConstraint, UnicodeText, Boolean, create_engine, insert, select,
)

from models import StudyUI

load_dotenv(override=True)


# --------------------------------------------------------------------------- #
# engine (same rule as auth.py: Azure SQL if AZURE_SQL_SERVER is set, else SQLite)
# --------------------------------------------------------------------------- #
def _make_engine():
    server = os.getenv("AZURE_SQL_SERVER")
    if not server:
        return create_engine("sqlite:///auth_dev.db", future=True)
    odbc = (
        f"Driver={{{os.getenv('AZURE_SQL_DRIVER', 'ODBC Driver 18 for SQL Server')}}};"
        f"Server=tcp:{server},1433;Database={os.getenv('AZURE_SQL_DATABASE')};"
        f"Uid={os.getenv('AZURE_SQL_USERNAME')};Pwd={os.getenv('AZURE_SQL_PASSWORD')};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=90;"
    )
    return create_engine(
        "mssql+pyodbc:///?odbc_connect=" + quote_plus(odbc), future=True, pool_pre_ping=True
    )


engine = _make_engine()
metadata = MetaData()

# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
studies = Table(
    "studies", metadata,
    Column("study_id", String(36), primary_key=True),
    Column("owner_user_id", String(36), nullable=False, index=True),
    Column("doc_id", String(64), nullable=False),                 # SHA-256 of the PDF bytes
    Column("blob_path", String(400)),
    Column("study_name", String(400)),
    Column("acronym", String(100)),
    Column("publication_year", Integer),
    Column("registry_id", String(50)),
    Column("funder", String(200)),
    Column("design_type", String(300)),
    Column("median_follow_up_years", Float),
    Column("patient_count", Integer),
    Column("noninferiority_margin", Float),
    Column("authors_conclusion", UnicodeText),
    Column("baseline_available", Boolean),
    Column("raw_json", UnicodeText, nullable=False),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("owner_user_id", "doc_id", name="uq_user_doc"),
)

arms = Table(
    "arms", metadata,
    Column("arm_id", String(36), primary_key=True),
    Column("study_id", String(36), ForeignKey("studies.study_id"), nullable=False, index=True),
    Column("name", String(200), nullable=False),
    Column("role", String(20), nullable=False),
    Column("dose", String(300)),
    Column("n_randomized", Integer),
    Column("n_itt", Integer),
    Column("n_per_protocol", Integer),
    Column("completed_n", Integer),
    Column("completed_pct", Float),
    Column("discontinued_drug_n", Integer),
    Column("discontinued_drug_pct", Float),
    Column("source_page", Integer),
)

outcomes = Table(
    "outcomes", metadata,
    Column("outcome_id", String(36), primary_key=True),
    Column("study_id", String(36), ForeignKey("studies.study_id"), nullable=False, index=True),
    Column("outcome", String(500), nullable=False),
    Column("outcome_group", String(30)),
    Column("outcome_type", String(10)),
    Column("is_primary", Boolean),
    Column("analysis_set", String(20)),
    Column("arm_a", String(200)),
    Column("arm_b", String(200)),
    Column("events_a", Integer), Column("events_b", Integer),
    Column("pct_a", Float), Column("pct_b", Float),
    Column("rate_a", Float), Column("rate_b", Float),
    Column("effect_measure", String(10)),
    Column("effect", Float), Column("ci_lower", Float), Column("ci_upper", Float),
    Column("p_value", Float),
    Column("p_value_text", String(60)),
    Column("p_value_type", String(20)),
    Column("adjusted_for", String(300)),
    Column("source_page", Integer),
)

baseline_items = Table(
    "baseline_items", metadata,
    Column("item_id", String(36), primary_key=True),
    Column("study_id", String(36), ForeignKey("studies.study_id"), nullable=False, index=True),
    Column("name", String(300), nullable=False),
    Column("category", String(30)),
    Column("arm", String(200)),
    Column("value_type", String(20)),
    Column("count", Integer), Column("percent", Float),
    Column("mean", Float), Column("sd", Float), Column("median", Float),
    Column("iqr", String(60)), Column("unit", String(40)), Column("text", String(300)),
    Column("source_page", Integer),
)

adverse_events = Table(
    "adverse_events", metadata,
    Column("ae_id", String(36), primary_key=True),
    Column("study_id", String(36), ForeignKey("studies.study_id"), nullable=False, index=True),
    Column("event", String(300), nullable=False),
    Column("arm", String(200)),
    Column("is_serious", Boolean),
    Column("patients_n", Integer), Column("patients_pct", Float), Column("events_n", Integer),
    Column("source_page", Integer),
)

continuous_outcomes = Table(
    "continuous_outcomes", metadata,
    Column("co_id", String(36), primary_key=True),
    Column("study_id", String(36), ForeignKey("studies.study_id"), nullable=False, index=True),
    Column("name", String(300), nullable=False),
    Column("timepoint", String(100)), Column("unit", String(60)),
    Column("arm_a", String(200)), Column("arm_b", String(200)),
    Column("value_a", Float), Column("value_b", Float), Column("sd_a", Float), Column("sd_b", Float),
    Column("difference", Float), Column("ci_lower", Float), Column("ci_upper", Float),
    Column("p_value", Float), Column("p_value_text", String(60)),
    Column("source_page", Integer),
)

deaths = Table(
    "deaths", metadata,
    Column("death_id", String(36), primary_key=True),
    Column("study_id", String(36), ForeignKey("studies.study_id"), nullable=False, index=True),
    Column("arm", String(200)), Column("cause", String(200)),
    Column("pct", Float), Column("n", Integer), Column("source_page", Integer),
)

buckets = Table(
    "buckets", metadata,
    Column("bucket_id", String(36), primary_key=True),
    Column("owner_user_id", String(36), nullable=False, index=True),
    Column("name", String(100), nullable=False),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("owner_user_id", "name", name="uq_user_bucket"),
)

bucket_studies = Table(      # a study can sit in many buckets
    "bucket_studies", metadata,
    Column("bucket_id", String(36), ForeignKey("buckets.bucket_id"), primary_key=True),
    Column("study_id", String(36), ForeignKey("studies.study_id"), primary_key=True),
)

medicines = Table(           # saved medicine profiles (web research), one per (user, medicine name)
    "medicines", metadata,
    Column("medicine_id", String(36), primary_key=True),
    Column("owner_user_id", String(36), nullable=False, index=True),
    Column("name", String(200), nullable=False),
    Column("profile_json", UnicodeText, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint("owner_user_id", "name", name="uq_user_medicine"),
)

metadata.create_all(engine)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _uid() -> str:
    return str(uuid.uuid4())


def _rows(items, study_id: str, fields: List[str], id_col: str, rename: Optional[dict] = None):
    """Turn a list of pydantic objects into insert-ready dict rows."""
    rename = rename or {}
    out = []
    for it in items:
        row = {id_col: _uid(), "study_id": study_id}
        for f in fields:
            row[rename.get(f, f)] = getattr(it, f)
        out.append(row)
    return out


ARM_F = ["name", "role", "dose", "n_randomized", "n_itt", "n_per_protocol", "completed_n",
         "completed_pct", "discontinued_drug_n", "discontinued_drug_pct", "source_page"]
OUT_F = ["outcome", "outcome_group", "outcome_type", "is_primary", "analysis_set", "arm_a", "arm_b",
         "events_a", "events_b", "pct_a", "pct_b", "rate_a", "rate_b", "effect_measure", "effect",
         "ci_lower", "ci_upper", "p_value", "p_value_text", "p_value_type", "adjusted_for", "source_page"]
BASE_F = ["name", "category", "arm", "value_type", "count", "percent", "mean", "sd", "median",
          "iqr", "unit", "text", "source_page"]
AE_F = ["event", "arm", "is_serious", "patients_n", "patients_pct", "events_n", "source_page"]
CO_F = ["name", "timepoint", "unit", "arm_a", "arm_b", "value_a", "value_b", "sd_a", "sd_b",
        "difference", "ci_lower", "ci_upper", "p_value", "p_value_text", "source_page"]
DEATH_F = ["arm", "cause", "pct", "n", "source_page"]


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def save_study(study: StudyUI, owner_user_id: str, doc_id: str,
               blob_path: Optional[str] = None) -> Tuple[str, bool]:
    """
    Save one extracted study for one user.
    Returns (study_id, created). created=False means this user already has this PDF
    (same doc_id) - nothing is inserted, no LLM cost needed again.
    """
    with engine.begin() as conn:
        existing = conn.execute(
            select(studies.c.study_id).where(
                (studies.c.owner_user_id == owner_user_id) & (studies.c.doc_id == doc_id)
            )
        ).first()
        if existing:
            return existing[0], False

        sid = _uid()
        conn.execute(insert(studies).values(
            study_id=sid, owner_user_id=owner_user_id, doc_id=doc_id, blob_path=blob_path,
            study_name=study.design.study_name, acronym=study.design.acronym,
            publication_year=study.design.publication_year, registry_id=study.design.registry_id,
            funder=study.design.funder, design_type=study.design.design_type,
            median_follow_up_years=study.design.median_follow_up_years,
            patient_count=study.patient_count,
            noninferiority_margin=study.statistics.noninferiority_margin,
            authors_conclusion=study.conclusions.authors_conclusion,
            baseline_available=study.baseline.available_in_paper,
            raw_json=study.model_dump_json(),
            created_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
        ))
        for tbl, items, fields, idc in (
            (arms, study.arms, ARM_F, "arm_id"),
            (outcomes, study.outcomes, OUT_F, "outcome_id"),
            (baseline_items, study.baseline.items, BASE_F, "item_id"),
            (adverse_events, study.adverse_events, AE_F, "ae_id"),
            (continuous_outcomes, study.continuous_outcomes, CO_F, "co_id"),
            (deaths, study.deaths, DEATH_F, "death_id"),
        ):
            rows = _rows(items, sid, fields, idc)
            if rows:
                conn.execute(insert(tbl), rows)
        return sid, True


def list_studies(owner_user_id: str) -> List[dict]:
    """Only THIS user's studies (this is also what the Compare picker will use)."""
    with engine.connect() as conn:
        res = conn.execute(
            select(studies.c.study_id, studies.c.study_name, studies.c.acronym,
                   studies.c.publication_year, studies.c.patient_count, studies.c.created_at)
            .where(studies.c.owner_user_id == owner_user_id)
            .order_by(studies.c.created_at.desc())
        )
        return [dict(r._mapping) for r in res]


def get_study(study_id: str, owner_user_id: str) -> Optional[StudyUI]:
    """Return the study only if it belongs to this user, else None."""
    with engine.connect() as conn:
        row = conn.execute(
            select(studies.c.raw_json).where(
                (studies.c.study_id == study_id) & (studies.c.owner_user_id == owner_user_id)
            )
        ).first()
    return StudyUI.model_validate_json(row[0]) if row else None


def get_blob_path(study_id: str, owner_user_id: str) -> Optional[str]:
    with engine.connect() as conn:
        row = conn.execute(
            select(studies.c.blob_path).where(
                (studies.c.study_id == study_id) & (studies.c.owner_user_id == owner_user_id)
            )
        ).first()
    return row[0] if row else None


def find_study_id(owner_user_id: str, doc_id: str) -> Optional[str]:
    """Study id if this user already saved this PDF (same hash), else None."""
    with engine.connect() as conn:
        row = conn.execute(
            select(studies.c.study_id).where(
                (studies.c.owner_user_id == owner_user_id) & (studies.c.doc_id == doc_id)
            )
        ).first()
    return row[0] if row else None


def delete_study(study_id: str, owner_user_id: str) -> bool:
    """Delete a study and all its child rows, only if it belongs to this user."""
    from sqlalchemy import delete
    with engine.begin() as conn:
        owned = conn.execute(
            select(studies.c.study_id).where(
                (studies.c.study_id == study_id) & (studies.c.owner_user_id == owner_user_id)
            )
        ).first()
        if not owned:
            return False
        for tbl in (bucket_studies, arms, outcomes, baseline_items, adverse_events, continuous_outcomes, deaths):
            conn.execute(delete(tbl).where(tbl.c.study_id == study_id))
        conn.execute(delete(studies).where(studies.c.study_id == study_id))
        return True


# --------------------------------------------------------------------------- #
# buckets: the user's own folders for studies (e.g. "Diabetes", "Cardio")
# --------------------------------------------------------------------------- #
def _own_bucket(conn, bucket_id: str, owner_user_id: str) -> bool:
    return conn.execute(select(buckets.c.bucket_id).where(
        (buckets.c.bucket_id == bucket_id) & (buckets.c.owner_user_id == owner_user_id))).first() is not None


def create_bucket(owner_user_id: str, name: str) -> Optional[str]:
    """Returns the new bucket id, or None if this user already has a bucket with that name."""
    name = " ".join(name.split())
    with engine.begin() as conn:
        for (n,) in conn.execute(select(buckets.c.name).where(buckets.c.owner_user_id == owner_user_id)):
            if n.lower() == name.lower():
                return None
        bid = _uid()
        conn.execute(insert(buckets).values(bucket_id=bid, owner_user_id=owner_user_id, name=name,
                                            created_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)))
        return bid


def list_buckets(owner_user_id: str) -> List[dict]:
    """[{bucket_id, name, study_ids}] for this user."""
    with engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(
            select(buckets.c.bucket_id, buckets.c.name).where(buckets.c.owner_user_id == owner_user_id)
            .order_by(buckets.c.created_at))]
        links = conn.execute(select(bucket_studies.c.bucket_id, bucket_studies.c.study_id)
                             .where(bucket_studies.c.bucket_id.in_([r["bucket_id"] for r in rows] or [""])))
        by = {}
        for b, st in links:
            by.setdefault(b, []).append(st)
    for r in rows:
        r["study_ids"] = by.get(r["bucket_id"], [])
    return rows


def rename_bucket(bucket_id: str, owner_user_id: str, name: str) -> str:
    """'ok', 'missing' or 'duplicate'."""
    from sqlalchemy import update
    name = " ".join(name.split())
    with engine.begin() as conn:
        if not _own_bucket(conn, bucket_id, owner_user_id):
            return "missing"
        for b, n in conn.execute(select(buckets.c.bucket_id, buckets.c.name).where(buckets.c.owner_user_id == owner_user_id)):
            if b != bucket_id and n.lower() == name.lower():
                return "duplicate"
        conn.execute(update(buckets).where(buckets.c.bucket_id == bucket_id).values(name=name))
        return "ok"


def delete_bucket(bucket_id: str, owner_user_id: str) -> bool:
    """Deletes the bucket only; the studies stay."""
    from sqlalchemy import delete
    with engine.begin() as conn:
        if not _own_bucket(conn, bucket_id, owner_user_id):
            return False
        conn.execute(delete(bucket_studies).where(bucket_studies.c.bucket_id == bucket_id))
        conn.execute(delete(buckets).where(buckets.c.bucket_id == bucket_id))
        return True


def add_to_bucket(bucket_id: str, owner_user_id: str, study_ids: List[str]) -> Optional[int]:
    """Adds this user's own studies to this user's own bucket. Returns how many were added, None if no such bucket."""
    with engine.begin() as conn:
        if not _own_bucket(conn, bucket_id, owner_user_id):
            return None
        mine = {r[0] for r in conn.execute(select(studies.c.study_id).where(
            (studies.c.owner_user_id == owner_user_id) & (studies.c.study_id.in_(study_ids or [""]))))}
        have = {r[0] for r in conn.execute(select(bucket_studies.c.study_id).where(bucket_studies.c.bucket_id == bucket_id))}
        new = sorted(mine - have)
        if new:
            conn.execute(insert(bucket_studies), [{"bucket_id": bucket_id, "study_id": x} for x in new])
        return len(new)


def remove_from_bucket(bucket_id: str, owner_user_id: str, study_id: str) -> bool:
    from sqlalchemy import delete
    with engine.begin() as conn:
        if not _own_bucket(conn, bucket_id, owner_user_id):
            return False
        conn.execute(delete(bucket_studies).where(
            (bucket_studies.c.bucket_id == bucket_id) & (bucket_studies.c.study_id == study_id)))
        return True


# --------------------------------------------------------------------------- #
# medicine profiles
# --------------------------------------------------------------------------- #
def save_medicine(owner_user_id: str, name: str, profile_json: str) -> str:
    """Insert or replace this user's profile for this medicine name (case-insensitive). Returns medicine_id."""
    from sqlalchemy import update
    name = " ".join(name.split())
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        for mid, n in conn.execute(select(medicines.c.medicine_id, medicines.c.name).where(medicines.c.owner_user_id == owner_user_id)):
            if n.lower() == name.lower():
                conn.execute(update(medicines).where(medicines.c.medicine_id == mid).values(profile_json=profile_json, updated_at=now))
                return mid
        mid = _uid()
        conn.execute(insert(medicines).values(medicine_id=mid, owner_user_id=owner_user_id, name=name, profile_json=profile_json, updated_at=now))
        return mid


def list_medicines(owner_user_id: str) -> List[dict]:
    with engine.connect() as conn:
        return [{"medicine_id": r.medicine_id, "name": r.name, "updated_at": r.updated_at.isoformat()} for r in conn.execute(
            select(medicines.c.medicine_id, medicines.c.name, medicines.c.updated_at)
            .where(medicines.c.owner_user_id == owner_user_id).order_by(medicines.c.name))]


def get_medicine(owner_user_id: str, medicine_id: str) -> Optional[dict]:
    with engine.connect() as conn:
        r = conn.execute(select(medicines).where((medicines.c.medicine_id == medicine_id) & (medicines.c.owner_user_id == owner_user_id))).first()
    if not r:
        return None
    import json as _j
    return {"medicine_id": r.medicine_id, "name": r.name, "updated_at": r.updated_at.isoformat(), "profile": _j.loads(r.profile_json)}


def delete_medicine(owner_user_id: str, medicine_id: str) -> bool:
    from sqlalchemy import delete
    with engine.begin() as conn:
        return conn.execute(delete(medicines).where((medicines.c.medicine_id == medicine_id) & (medicines.c.owner_user_id == owner_user_id))).rowcount > 0
