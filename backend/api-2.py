"""api.py - FastAPI backend + static UI.

Run:  uvicorn api:app --reload      (run inside backend/)  then open http://127.0.0.1:8000
Every /api endpoint takes the user id from the login token, never from the browser.
"""
import hashlib
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from auth import get_current_user, router as auth_router
from models import StudyUI, display_label
from verdict import get_verdict, overall_verdict

load_dotenv(override=True)
BASE = Path(__file__).parent          # backend/
ROOT = BASE.parent                      # project root
MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
DEMO = ROOT / "application_work" / "golden" / "tecos_golden.json"

app = FastAPI(title="Pharma Study Analyser")
app.include_router(auth_router)


def _study_payload(study: StudyUI, study_id: str) -> dict:
    margin = study.statistics.noninferiority_margin
    data = study.model_dump()
    for row, o in zip(data["outcomes"], study.outcomes):
        row["verdict"] = get_verdict(o, margin)
    data["study_id"] = study_id
    data["label"] = display_label(study.design)
    data["verdict"] = overall_verdict(study)
    return data


@app.post("/api/studies")
def upload_study(file: UploadFile = File(...), user_id: str = Depends(get_current_user)):
    data = file.file.read()
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"File is larger than {MAX_MB} MB.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Please upload a PDF file.")
    doc_id = hashlib.sha256(data).hexdigest()
    existing = db.find_study_id(user_id, doc_id)
    if existing:                      # same PDF again: no LLM cost
        return {"study_id": existing, "created": False}
    from extract import extract_study
    try:
        study = extract_study(data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Extraction failed: {e}")
    from storage import upload_pdf
    blob_path = upload_pdf(user_id, file.filename or "study.pdf", data)
    study_id, created = db.save_study(study, user_id, doc_id, blob_path)
    indexed = None
    if created:                       # chunks for the chat; a failure here must not lose the extraction
        try:
            import rag
            from extract import pages_text
            indexed = rag.index_study(user_id, study_id, pages_text(data))
        except Exception as e:
            indexed = f"not indexed: {e}"
    return {"study_id": study_id, "created": created, "indexed": indexed}


@app.post("/api/demo")
def load_demo(user_id: str = Depends(get_current_user)):
    """Saves the TECOS golden record for this user, so the UI can be tried without the LLM."""
    if not DEMO.exists():
        raise HTTPException(404, "Demo file not found: application_work/golden/tecos_golden.json")
    study = StudyUI.model_validate_json(DEMO.read_text(encoding="utf-8"))
    study_id, created = db.save_study(study, user_id, "demo-tecos")
    return {"study_id": study_id, "created": created}


@app.get("/api/studies")
def list_my_studies(user_id: str = Depends(get_current_user)):
    rows = db.list_studies(user_id)
    for r in rows:
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        r["label"] = r["acronym"] or (r["study_name"] or "Study").split(":")[0].strip()[:48]
    return rows


@app.get("/api/studies/{study_id}")
def get_my_study(study_id: str, user_id: str = Depends(get_current_user)):
    study = db.get_study(study_id, user_id)
    if study is None:
        raise HTTPException(404, "Study not found.")
    return _study_payload(study, study_id)


@app.delete("/api/studies/{study_id}")
def delete_my_study(study_id: str, user_id: str = Depends(get_current_user)):
    if not db.delete_study(study_id, user_id):
        raise HTTPException(404, "Study not found.")
    try:
        import rag, cache
        rag.delete_study_chunks(user_id, study_id)
        cache.clear_study(user_id, study_id)
        import memory
        memory.reset(user_id, f"study:{study_id}")
    except Exception:
        pass
    return {"deleted": True}


class ChatBody(BaseModel):
    question: str


class CompareBody(BaseModel):
    question: str
    study_ids: list[str] = []


def _check_question(q: str) -> str:
    q = q.strip()
    if not (2 <= len(q) <= 500):
        raise HTTPException(400, "Question must be 2 to 500 characters.")
    return q


def _facts(label: str, st: StudyUI) -> str:
    """Compact numbers from the database for the compare chat (exact, no LLM)."""
    margin = st.statistics.noninferiority_margin
    arms = "; ".join(f"{a.name} (n={a.n})" for a in st.arms)
    lines = [f"[{label}] {st.design.study_name} ({st.design.publication_year or 'year n/a'}). Design: {st.design.design_type or 'n/a'}. Arms: {arms}."]
    for o in st.outcomes:
        if o.is_primary and o.effect is not None:
            v = get_verdict(o, margin)
            lines.append(f"[{label}] Primary outcome ({o.analysis_set or 'set n/a'}): {o.outcome}; {o.effect_measure} {o.effect} "
                         f"({o.ci_lower}-{o.ci_upper}); p {o.p_value_text or o.p_value}" + (f"; verdict: {v['label']}" if v else ""))
    return "\n".join(lines)


@app.post("/api/studies/{study_id}/chat")
def chat(study_id: str, body: ChatBody, user_id: str = Depends(get_current_user)):
    q = _check_question(body.question)
    if db.get_study(study_id, user_id) is None:        # ownership check
        raise HTTPException(404, "Study not found.")
    import chat_graph
    try:
        return chat_graph.ask(user_id, q, [study_id], f"study:{study_id}")
    except Exception as e:
        raise HTTPException(500, f"Chat failed: {e}")


@app.get("/api/studies/{study_id}/chat")
def chat_history(study_id: str, user_id: str = Depends(get_current_user)):
    if db.get_study(study_id, user_id) is None:
        raise HTTPException(404, "Study not found.")
    import memory
    return {"turns": memory.load(user_id, f"study:{study_id}")["turns"]}


@app.delete("/api/studies/{study_id}/chat")
def chat_reset(study_id: str, user_id: str = Depends(get_current_user)):
    import memory
    memory.reset(user_id, f"study:{study_id}")
    return {"reset": True}


@app.post("/api/compare/chat")
def compare_chat(body: CompareBody, bucket: str = "all", user_id: str = Depends(get_current_user)):
    q = _check_question(body.question)
    mine = {r["study_id"] for r in db.list_studies(user_id)}
    ids = [i for i in (body.study_ids or sorted(mine)) if i in mine][:6]      # only this user's studies
    if len(ids) < 2:
        raise HTTPException(400, "Pick at least 2 of your studies to compare.")
    labels, facts = {}, []
    for i in ids:
        st = db.get_study(i, user_id)
        labels[i] = display_label(st.design)
        facts.append(_facts(labels[i], st))
    import chat_graph
    try:
        return chat_graph.ask(user_id, q, ids, "compare", labels, "\n".join(facts), memory_scope=f"compare:{bucket}")
    except Exception as e:
        raise HTTPException(500, f"Chat failed: {e}")


@app.get("/api/compare/chat")
def compare_history(bucket: str = "all", user_id: str = Depends(get_current_user)):
    import memory
    return {"turns": memory.load(user_id, f"compare:{bucket}")["turns"]}


@app.delete("/api/compare/chat")
def compare_reset(bucket: str = "all", user_id: str = Depends(get_current_user)):
    import memory
    memory.reset(user_id, f"compare:{bucket}")
    return {"reset": True}


class BucketBody(BaseModel):
    name: str


class BucketStudies(BaseModel):
    study_ids: list[str]


def _bucket_name(name: str) -> str:
    n = " ".join(name.split())
    if not (1 <= len(n) <= 60):
        raise HTTPException(400, "Bucket name must be 1 to 60 characters.")
    return n


@app.get("/api/buckets")
def my_buckets(user_id: str = Depends(get_current_user)):
    return db.list_buckets(user_id)


@app.post("/api/buckets", status_code=201)
def new_bucket(body: BucketBody, user_id: str = Depends(get_current_user)):
    bid = db.create_bucket(user_id, _bucket_name(body.name))
    if bid is None:
        raise HTTPException(409, "You already have a bucket with this name.")
    return {"bucket_id": bid}


@app.patch("/api/buckets/{bucket_id}")
def rename_my_bucket(bucket_id: str, body: BucketBody, user_id: str = Depends(get_current_user)):
    r = db.rename_bucket(bucket_id, user_id, _bucket_name(body.name))
    if r == "missing":
        raise HTTPException(404, "Bucket not found.")
    if r == "duplicate":
        raise HTTPException(409, "You already have a bucket with this name.")
    return {"renamed": True}


@app.delete("/api/buckets/{bucket_id}")
def delete_my_bucket(bucket_id: str, user_id: str = Depends(get_current_user)):
    if not db.delete_bucket(bucket_id, user_id):
        raise HTTPException(404, "Bucket not found.")
    return {"deleted": True}


@app.post("/api/buckets/{bucket_id}/studies")
def bucket_add(bucket_id: str, body: BucketStudies, user_id: str = Depends(get_current_user)):
    n = db.add_to_bucket(bucket_id, user_id, body.study_ids)
    if n is None:
        raise HTTPException(404, "Bucket not found.")
    return {"added": n}


@app.delete("/api/buckets/{bucket_id}/studies/{study_id}")
def bucket_remove(bucket_id: str, study_id: str, user_id: str = Depends(get_current_user)):
    if not db.remove_from_bucket(bucket_id, user_id, study_id):
        raise HTTPException(404, "Bucket not found.")
    return {"removed": True}


# ---------------------------------------------------------------- sales & marketing
class MarketingBody(BaseModel):
    study_ids: list[str]


class ExportBody(BaseModel):
    kind: str
    content: dict
    studies: list[str] = []


def _marketing_items(ids: list[str], user_id: str):
    mine = {r["study_id"] for r in db.list_studies(user_id)}
    ids = [i for i in dict.fromkeys(ids) if i in mine]          # only this user's studies, no duplicates
    if not 1 <= len(ids) <= 3:
        raise HTTPException(400, "Pick 1 to 3 of your studies.")
    items = []
    for i in ids:
        st = db.get_study(i, user_id)
        items.append((display_label(st.design), st))
    return items


@app.post("/api/marketing/{kind}")
def marketing_generate(kind: str, body: MarketingBody, user_id: str = Depends(get_current_user)):
    import marketing
    if kind not in marketing.KINDS:
        raise HTTPException(404, "Unknown output type.")
    items = _marketing_items(body.study_ids, user_id)
    try:
        return marketing.generate(kind, items)
    except Exception as e:
        raise HTTPException(500, f"Could not generate: {e}")


@app.post("/api/marketing-export")
def marketing_export(body: ExportBody, user_id: str = Depends(get_current_user)):
    import marketing
    from fastapi.responses import Response
    try:
        if body.kind == "brochure":
            data, name, mt = marketing.to_docx(body.content, body.studies), "doctor_brochure.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif body.kind == "slides":
            data, name, mt = marketing.to_pptx(body.content, body.studies), "study_slides.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        else:
            raise HTTPException(400, "Only brochure and slides can be downloaded.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Could not build the file: {e}")
    return Response(data, media_type=mt, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------------------------------------------------------------- PPT agent (human in the loop)
class PptReviseBody(BaseModel):
    n: int | None = None
    request: str = ""
    notes: dict[str, str] = {}


class PptApproveBody(BaseModel):
    notes: dict[str, str] = {}
    timed_out: bool = False


@app.post("/api/ppt/start")
def ppt_start(body: MarketingBody, user_id: str = Depends(get_current_user)):
    import ppt_agent
    items = _marketing_items(body.study_ids, user_id)
    try:
        return ppt_agent.start(user_id, items)
    except Exception as e:
        raise HTTPException(500, f"Could not plan the slides: {e}")


@app.post("/api/ppt/{sid}/revise")
def ppt_revise(sid: str, body: PptReviseBody, user_id: str = Depends(get_current_user)):
    import ppt_agent
    try:
        return ppt_agent.revise(user_id, sid, body.n, body.request, body.notes)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\""))
    except Exception as e:
        raise HTTPException(500, f"Could not re-plan: {e}")


@app.post("/api/ppt/{sid}/approve")
def ppt_approve(sid: str, body: PptApproveBody, user_id: str = Depends(get_current_user)):
    import ppt_agent
    try:
        return ppt_agent.approve(user_id, sid, body.notes, body.timed_out)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\""))
    except Exception as e:
        raise HTTPException(500, f"Could not create the slides: {e}")


# ---------------------------------------------------------------- medicine pages (web research)
class MedicineBody(BaseModel):
    name: str
    country: str = "India"


def _library_matches(user_id: str, generic: str) -> list[dict]:
    """The user's own uploaded studies that mention this medicine (by name in title or arm names)."""
    word = (generic or "").lower().split()[0] if generic else ""
    if len(word) < 4:
        return []
    out = []
    for r in db.list_studies(user_id):
        st = db.get_study(r["study_id"], user_id)
        hay = " ".join([st.design.study_name or "", st.design.acronym or ""] + [a.name for a in st.arms]).lower()
        if word in hay:
            out.append({"study_id": r["study_id"], "label": display_label(st.design), "year": st.design.publication_year})
    return out


@app.post("/api/medicines")
def medicine_create(body: MedicineBody, user_id: str = Depends(get_current_user)):
    import json as _j
    import medicine_agent
    name = " ".join(body.name.split())
    if not 2 <= len(name) <= 80:
        raise HTTPException(400, "Type a medicine name (2 to 80 characters).")
    try:
        profile = medicine_agent.research(name, (body.country or "India")[:40])
    except Exception as e:
        raise HTTPException(500, f"Could not research this medicine: {e}")
    mid = db.save_medicine(user_id, name, _j.dumps(profile))
    return {"medicine_id": mid, "name": name, "profile": profile, "library": _library_matches(user_id, profile.get("generic_name", name))}


@app.get("/api/medicines")
def medicine_list(user_id: str = Depends(get_current_user)):
    return db.list_medicines(user_id)


@app.get("/api/medicines/{medicine_id}")
def medicine_get(medicine_id: str, user_id: str = Depends(get_current_user)):
    m = db.get_medicine(user_id, medicine_id)
    if not m:
        raise HTTPException(404, "Medicine not found.")
    m["library"] = _library_matches(user_id, m["profile"].get("generic_name", m["name"]))
    return m


@app.delete("/api/medicines/{medicine_id}")
def medicine_delete(medicine_id: str, user_id: str = Depends(get_current_user)):
    if not db.delete_medicine(user_id, medicine_id):
        raise HTTPException(404, "Medicine not found.")
    return {"deleted": True}


# UI (must be last)
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="ui")
