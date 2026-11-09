"""rag.py - PDF chunks in Azure AI Search (vector + keyword, hybrid), one filter per user and per study.

Needs in .env:
    AZURE_SEARCH_ENDPOINT, AZURE_SEARCH_KEY, AZURE_SEARCH_INDEX_NAME (e.g. pharma-study-chunks)
    OPENAI_API_KEY, OPENAI_EMBEDDING_MODEL (text-embedding-3-small), EMBEDDING_DIMENSIONS (1536)
    CHUNK_SIZE (1000), CHUNK_OVERLAP (150)
The index is created automatically the first time it is needed.
Every chunk carries user_id + study_id, and every search filters on both, so one user can never
retrieve another user's paper.
"""
import os
from typing import List

from dotenv import load_dotenv

load_dotenv(override=True)

_index_ready = False


def _cfg():
    return (os.environ["AZURE_SEARCH_ENDPOINT"], os.environ["AZURE_SEARCH_KEY"],
            os.getenv("AZURE_SEARCH_INDEX_NAME", "pharma-study-chunks"))


def _dims() -> int:
    return int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))


def _q(s: str) -> str:
    return str(s).replace("'", "''")          # OData string escape


def _flt(user_id: str, study_id: str) -> str:
    return f"user_id eq '{_q(user_id)}' and study_id eq '{_q(study_id)}'"


def _embedder():
    from langchain_openai import OpenAIEmbeddings
    return OpenAIEmbeddings(model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
                            api_key=os.environ["OPENAI_API_KEY"])


def _search_client():
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents import SearchClient
    ep, key, name = _cfg()
    return SearchClient(ep, name, AzureKeyCredential(key))


def ensure_index() -> None:
    global _index_ready
    if _index_ready:
        return
    from azure.core.credentials import AzureKeyCredential
    from azure.search.documents.indexes import SearchIndexClient
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration, SearchField, SearchFieldDataType, SearchIndex,
        SearchableField, SimpleField, VectorSearch, VectorSearchProfile)
    ep, key, name = _cfg()
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="user_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="study_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="page", type=SearchFieldDataType.Int32, filterable=True),
        SearchableField(name="content", type=SearchFieldDataType.String),
        SearchField(name="content_vector", type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                    searchable=True, vector_search_dimensions=_dims(), vector_search_profile_name="vprofile"),
    ]
    vs = VectorSearch(algorithms=[HnswAlgorithmConfiguration(name="hnsw")],
                      profiles=[VectorSearchProfile(name="vprofile", algorithm_configuration_name="hnsw")])
    SearchIndexClient(ep, AzureKeyCredential(key)).create_or_update_index(
        SearchIndex(name=name, fields=fields, vector_search=vs))
    _index_ready = True


def split_pages(pages: List[str]) -> List[dict]:
    """[(page text)] -> [{page, text}] chunks, never crossing a page boundary (so a citation is exact)."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    sp = RecursiveCharacterTextSplitter(chunk_size=int(os.getenv("CHUNK_SIZE", "1000")),
                                        chunk_overlap=int(os.getenv("CHUNK_OVERLAP", "150")))
    out = []
    for pno, text in enumerate(pages, start=1):
        for piece in sp.split_text(text or ""):
            if len(piece.strip()) > 30:
                out.append({"page": pno, "text": piece})
    return out


def index_study(user_id: str, study_id: str, pages: List[str]) -> int:
    """Chunk + embed + upload. Returns number of chunks."""
    ensure_index()
    chunks = split_pages(pages)
    if not chunks:
        return 0
    emb = _embedder()
    client = _search_client()
    for i in range(0, len(chunks), 64):
        batch = chunks[i:i + 64]
        vecs = emb.embed_documents([c["text"] for c in batch])
        docs = [{"id": f"{study_id}-{c['page']}-{i + j}", "user_id": user_id, "study_id": study_id,
                 "page": c["page"], "content": c["text"], "content_vector": v}
                for j, (c, v) in enumerate(zip(batch, vecs))]
        client.upload_documents(docs)
    return len(chunks)


def search_many(user_id: str, study_ids: List[str], query: str, k: int = 30) -> List[dict]:
    """Hybrid (vector + keyword) search inside this user's chosen studies. Returns [{study_id, page, text}]."""
    from azure.search.documents.models import VectorizedQuery
    ensure_index()
    ids = ",".join(str(i).replace(",", "") for i in study_ids)
    flt = f"user_id eq '{_q(user_id)}' and search.in(study_id, '{_q(ids)}', ',')"
    vec = _embedder().embed_query(query)
    res = _search_client().search(
        search_text=query,
        vector_queries=[VectorizedQuery(vector=vec, k_nearest_neighbors=k, fields="content_vector")],
        filter=flt, top=k, select=["study_id", "page", "content"])
    return [{"study_id": r["study_id"], "page": r["page"], "text": r["content"], "score": r.get("@search.score")} for r in res]


def search(user_id: str, study_id: str, query: str, k: int = 30) -> List[dict]:
    """One study (kept for simple use)."""
    return search_many(user_id, [study_id], query, k)


def delete_study_chunks(user_id: str, study_id: str) -> int:
    ensure_index()
    client = _search_client()
    ids = [{"id": r["id"]} for r in client.search(search_text="*", filter=_flt(user_id, study_id),
                                                  select=["id"], top=1000)]
    if ids:
        client.delete_documents(ids)
    return len(ids)
