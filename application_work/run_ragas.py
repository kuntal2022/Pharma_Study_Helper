"""run_ragas.py - run the golden questions through the real chat graph and score them with RAGAS.

Run from the project root (needs the same .env as the app: OpenAI + Azure Search):
    python application_work/run_ragas.py --user-id <user id that uploaded the studies>
Fill application_work/golden/study_map.json first (study name in the CSV -> study_id in the app). Studies still set to
REPLACE_WITH_STUDY_ID are skipped. Add --limit 5 for a quick smoke test.

Answerable questions -> RAGAS faithfulness, answer relevancy, context precision, context recall + number recall
Unanswerable questions -> abstain rate (the answer must say it is not in the paper); anything else is a hallucination.
The cache and chat memory are switched off here, so every question runs the full RAG path.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["SEMANTIC_CACHE_ENABLED"] = "false"
sys.path.insert(0, str(ROOT / "backend"))

import pandas as pd
from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=True)

import chat_graph  # noqa: E402
import memory      # noqa: E402

chat_graph.cache.get = lambda key: None                 # no cache reads or writes
chat_graph.cache.put = lambda *a, **k: None
chat_graph.cache.sem_get = lambda *a, **k: None
chat_graph.cache.sem_put = lambda *a, **k: None
memory.load = lambda *a, **k: {"summary": "", "turns": [], "compressions": 0}    # no chat memory (empty, same shape)

memory.add_turn = lambda *a, **k: None

ABSTAIN = re.compile(r"not in the paper|not (stated|reported|mentioned|provided|available|included)|"
                     r"(cannot|can't|could not|couldn't|does not|doesn't|do not|don't|did not|didn't) (find|contain|report|state|provide|specify|mention|include|give)|supplement|"
                     r"no (information|data)", re.I)
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _shim_vertexai():
    """ragas 0.4.3 still imports langchain_community's Vertex AI classes, which langchain-community 0.4.x removed.
    We never use Vertex AI, so give ragas empty stand-ins just for the import."""
    import types
    name = "langchain_community.chat_models.vertexai"
    try:
        __import__(name)
    except Exception:
        m = types.ModuleType(name)
        m.ChatVertexAI = type("ChatVertexAI", (), {})
        sys.modules[name] = m
    import langchain_community.llms as L
    try:
        L.VertexAI
    except Exception:
        L.VertexAI = type("VertexAI", (), {})


def numbers(t: str) -> set:
    return {n.replace(",", "").rstrip(".") for n in NUM.findall(re.sub(r"\[[^\]]*\]", " ", t)) if len(n.replace(",", "")) > 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--csv", default=str(ROOT / "application_work/golden/eval_dataset.csv"))
    ap.add_argument("--study-map", default=str(ROOT / "application_work/golden/study_map.json"))
    ap.add_argument("--limit", type=int, default=0, help="only the first N questions per study (smoke test)")
    ap.add_argument("--out", default=str(ROOT / "application_work/golden/ragas_results"))
    a = ap.parse_args()

    smap = {k: v for k, v in json.load(open(a.study_map, encoding="utf-8")).items() if not v.startswith("REPLACE")}
    df = pd.read_csv(a.csv, encoding="utf-8-sig", keep_default_na=False)
    df = df[df["study"].isin(smap)]
    if a.limit:
        df = df.groupby("study", group_keys=False).head(a.limit)
    if df.empty:
        sys.exit("No questions to run: fill application_work/golden/study_map.json with real study ids.")

    graph = chat_graph.build_graph()
    rows = []
    for r in df.to_dict("records"):
        sid = smap[r["study"]]
        t = time.time()
        out = graph.invoke({"user_id": a.user_id, "scope": f"study:{sid}", "study_ids": [sid], "labels": {}, "facts": "",
                            "question": r["question"], "mem_scope": f"study:{sid}", "t0": t})
        rows.append({**r, "answer": out.get("answer", ""), "contexts": [c["text"] for c in out.get("contexts") or []],
                     "route": out.get("route"), "blocked": bool(out.get("blocked")),
                     "ungrounded": out.get("ungrounded") or [], "latency_s": round(time.time() - t, 1)})
        print(f"{r['id']} {r['study'][:10]:10} {rows[-1]['latency_s']:>5}s  {out.get('answer', '')[:70]!r}", flush=True)
    res = pd.DataFrame(rows)

    # ---- unanswerable: did it abstain? ----
    un = res[res["answerable"] == "no"].copy()
    un["abstained"] = un["answer"].apply(lambda t: bool(ABSTAIN.search(t)))

    # ---- answerable: RAGAS + number recall ----
    an = res[(res["answerable"] == "yes") & res["contexts"].apply(bool)].copy()
    an["number_recall"] = an.apply(lambda x: (len(numbers(x["expected_answer"]) & numbers(x["answer"])) /
                                              max(1, len(numbers(x["expected_answer"])))), axis=1)
    if not an.empty:
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        _shim_vertexai()
        from ragas import EvaluationDataset, evaluate
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall, ResponseRelevancy
        # judge + embeddings run on Azure (OpenAI-compatible /openai/v1/ endpoint, same style as test.ipynb)
        az_base = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/") + "/openai/v1/"
        az_key = os.environ["AZURE_OPENAI_API_KEY"]
        judge = LangchainLLMWrapper(ChatOpenAI(model=os.getenv("AZURE_JUDGE_DEPLOYMENT", "gpt-4.1-mini"),
                                               api_key=az_key, base_url=az_base,
                                               use_responses_api=False))
        emb = LangchainEmbeddingsWrapper(OpenAIEmbeddings(model=os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"),
                                                          api_key=az_key, base_url=az_base,
                                                          check_embedding_ctx_length=False))
        # pre-flight: call judge + embeddings once so the REAL error is visible (RAGAS hides it and gives NaN)
        try:
            _j = ChatOpenAI(model=os.getenv("AZURE_JUDGE_DEPLOYMENT", "gpt-4.1-mini"), api_key=az_key,
                            base_url=az_base, use_responses_api=False)
            print("JUDGE TEST ->", _j.invoke("Say OK").content[:50])
        except Exception as e:
            print("JUDGE TEST FAILED ->", type(e).__name__, str(e)[:600])
        try:
            _e = OpenAIEmbeddings(model=os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small"),
                                  api_key=az_key, base_url=az_base, check_embedding_ctx_length=False)
            print("EMBED TEST -> dim", len(_e.embed_query("hello")))
        except Exception as e:
            print("EMBED TEST FAILED ->", type(e).__name__, str(e)[:600])
        ds = EvaluationDataset.from_list([{"user_input": x["question"], "response": x["answer"],
                                           "retrieved_contexts": x["contexts"], "reference": x["expected_answer"]}
                                          for x in an.to_dict("records")])
        sc = evaluate(ds, metrics=[Faithfulness(), ResponseRelevancy(), LLMContextPrecisionWithReference(), LLMContextRecall()],
                      llm=judge, embeddings=emb).to_pandas()
        for c in [c for c in sc.columns if c not in ("user_input", "response", "retrieved_contexts", "reference")]:
            an[c] = sc[c].values

    Path(a.out).mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    keep = ["id", "study", "category", "question", "expected_answer", "answer", "latency_s", "ungrounded"]
    an.drop(columns=["contexts"]).to_csv(f"{a.out}/answerable_{stamp}.csv", index=False, encoding="utf-8-sig")
    un[keep + ["abstained"]].to_csv(f"{a.out}/unanswerable_{stamp}.csv", index=False, encoding="utf-8-sig")

    print("\n=========== SUMMARY ===========")
    if not an.empty:
        num = an.select_dtypes("number").drop(columns=["page", "latency_s"], errors="ignore")
        print(num.mean().round(3).to_string())
        print("\nper study:\n", an.groupby("study")[list(num.columns)].mean().round(3).to_string())
    if not un.empty:
        print(f"\nabstain rate on 'not in paper' questions: {un['abstained'].mean():.0%} ({int(un['abstained'].sum())}/{len(un)})")
        for x in un[~un["abstained"]].to_dict("records"):
            print(f"  HALLUCINATION? {x['id']}: {x['answer'][:100]!r}")
    print(f"\nlatency avg {res['latency_s'].mean():.1f}s | blocked by guard: {int(res['blocked'].sum())} | saved in {a.out}")


if __name__ == "__main__":
    main()
