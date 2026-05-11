"""
UC1 – Regulatory Intelligence Chatbot
Uses a local FAISS vector index (built from Chunks_Complete.pkl) for knowledge-base
retrieval, and Azure OpenAI for query transformation + RAG response streaming.

On first run the FAISS index is built by embedding all chunks and saved to disk.
Subsequent runs load the saved index — no Pinecone or external vector DB needed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts.chat import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    SystemMessagePromptTemplate,
)

from utils.llm_client import get_chat_llm, get_config, get_embeddings, read_asset


# ── Lazy-loaded singletons ────────────────────────────────────────────────────

_vectorstore = None
_df_chunks: pd.DataFrame | None = None


def _get_df_chunks() -> pd.DataFrame:
    global _df_chunks
    if _df_chunks is None:
        pkl_path = get_config()["uc1"]["chunks_pkl_path"]
        _df_chunks = pd.read_pickle(pkl_path)
    return _df_chunks


def _get_vectorstore():
    """
    Load the FAISS index from disk, or build it from Chunks_Complete.pkl if it
    does not exist yet. The index is saved to `uc1.faiss_index_path` in config.json.
    """
    global _vectorstore
    if _vectorstore is not None:
        return _vectorstore

    from langchain_community.vectorstores import FAISS

    cfg = get_config()["uc1"]
    index_path = cfg.get("faiss_index_path", "uc1_faiss_index")
    embeddings = get_embeddings()

    # ── Load existing index ────────────────────────────────────────────────────
    if Path(index_path).exists():
        _vectorstore = FAISS.load_local(
            index_path, embeddings, allow_dangerous_deserialization=True
        )
        return _vectorstore

    # ── Build index from pkl ───────────────────────────────────────────────────
    print("[UC1] FAISS index not found — building from Chunks_Complete.pkl …")
    df = _get_df_chunks()

    from langchain_core.documents import Document

    docs = [
        Document(
            page_content=row["ChunkText"],
            metadata={
                "ChunkID": str(row["ChunkID"]),
                "Title": str(row.get("Title", "")),
                "PageNumber": int(row.get("PageNumber", 0)),
                "Chunk": int(row.get("Chunk", 0)),
            },
        )
        for _, row in df.iterrows()
    ]

    print(f"[UC1] Embedding {len(docs)} chunks in batches (rate-limit aware, with checkpointing) …")
    import json as _json
    import time

    BATCH = 200   # docs per batch — stays within S0 rate limits
    SLEEP = 5     # seconds between batches
    SAVE_EVERY = 5  # save checkpoint every N batches

    checkpoint_path = Path(index_path + "_partial")
    progress_file = checkpoint_path / ".build_progress"
    start_batch = 0

    # ── Resume from checkpoint if one exists ──────────────────────────────────
    if checkpoint_path.exists() and progress_file.exists():
        try:
            prog = _json.loads(progress_file.read_text())
            start_batch = prog.get("next_batch", 0)
            print(f"[UC1] Resuming from batch {start_batch + 1} …")
            _vectorstore = FAISS.load_local(
                str(checkpoint_path), embeddings, allow_dangerous_deserialization=True
            )
        except Exception:
            start_batch = 0
            _vectorstore = None

    total_batches = (len(docs) + BATCH - 1) // BATCH
    for batch_idx in range(start_batch, total_batches):
        i = batch_idx * BATCH
        batch = docs[i : i + BATCH]
        print(f"[UC1] Batch {batch_idx + 1}/{total_batches} ({len(batch)} docs)…")
        for attempt in range(6):
            try:
                if _vectorstore is None:
                    _vectorstore = FAISS.from_documents(batch, embeddings)
                else:
                    _vectorstore.add_documents(batch)
                break
            except Exception as e:
                err = str(e)
                if "429" in err or "RateLimit" in err:
                    wait = SLEEP * (2 ** attempt)
                    print(f"[UC1] Rate limit — waiting {wait}s …")
                    time.sleep(wait)
                elif attempt < 3 and ("Connection" in err or "getaddrinfo" in err or "Timeout" in err):
                    wait = 10 * (attempt + 1)
                    print(f"[UC1] Connection error — retrying in {wait}s …")
                    time.sleep(wait)
                else:
                    raise

        # ── Save checkpoint every SAVE_EVERY batches ──────────────────────────
        if (batch_idx + 1) % SAVE_EVERY == 0 or batch_idx == total_batches - 1:
            checkpoint_path.mkdir(parents=True, exist_ok=True)
            _vectorstore.save_local(str(checkpoint_path))
            progress_file.write_text(_json.dumps({"next_batch": batch_idx + 1, "total": total_batches}))
            print(f"[UC1] Checkpoint saved at batch {batch_idx + 1}/{total_batches}")

        if i + BATCH < len(docs):
            time.sleep(SLEEP)

    # ── Move checkpoint to final location ────────────────────────────────────
    import shutil
    if Path(index_path).exists():
        shutil.rmtree(index_path)
    shutil.copytree(str(checkpoint_path), index_path)
    shutil.rmtree(str(checkpoint_path))
    _vectorstore = FAISS.load_local(index_path, embeddings, allow_dangerous_deserialization=True)
    _vectorstore = _vectorstore  # re-assign to update global via outer scope
    print(f"[UC1] FAISS index saved to '{index_path}'")
    print(f"[UC1] FAISS index saved to '{index_path}'")
    return _vectorstore


# ── Retrieval ─────────────────────────────────────────────────────────────────

def _lookup_related_chunks(chunk_id: str, pages_returned: int) -> pd.DataFrame:
    df = _get_df_chunks()
    target = df[df["ChunkID"] == chunk_id]
    if target.empty:
        return pd.DataFrame()

    title = target.iloc[0]["Title"]
    page = target.iloc[0]["PageNumber"]
    min_page = df[df["Title"] == title]["PageNumber"].min()
    max_page = df[df["Title"] == title]["PageNumber"].max()
    page_range = [
        p
        for p in [page - pages_returned, page, page + pages_returned]
        if min_page <= p <= max_page
    ]
    return df[(df["Title"] == title) & (df["PageNumber"].isin(page_range))]


def _reconstruct_text(df: pd.DataFrame) -> str:
    return " ".join(df.sort_values("Chunk")["ChunkText"].tolist())


def search_and_reconstruct(query: str) -> list[dict]:
    cfg = get_config()["uc1"]
    k = cfg.get("top_k", 5)
    pages_returned = cfg.get("pages_returned", 3)

    vs = _get_vectorstore()

    # Returns List[Tuple[Document, float]] — scores are relevance in [0, 1]
    hits = vs.similarity_search_with_relevance_scores(query, k=k)

    results = []
    for doc, score in hits:
        meta = doc.metadata
        chunk_id = meta.get("ChunkID", "")
        related = _lookup_related_chunks(chunk_id, pages_returned)
        results.append(
            {
                "Title": meta.get("Title", ""),
                "Score": round(score, 4),
                "PageNumber": meta.get("PageNumber", ""),
                "ReconstructedText": _reconstruct_text(related) if not related.empty else doc.page_content,
            }
        )
    return results


# ── LLM helpers ───────────────────────────────────────────────────────────────

def transform_query(history: list, system_msg: str, glossary: str) -> str:
    cfg = get_config()["uc1"]
    llm = get_chat_llm(
        model_key="chat_deployment",
        temperature=cfg.get("qte_temperature", 0.5),
        max_tokens=cfg.get("qte_max_tokens", 200),
    )
    user_tpl = "## Conversation to date: {conversationToDate}\n\n## Create Optimised Query"
    prompt = ChatPromptTemplate.from_messages(
        [
            SystemMessagePromptTemplate.from_template(system_msg),
            HumanMessagePromptTemplate.from_template(user_tpl),
        ]
    )
    chain = prompt | llm | StrOutputParser()
    return chain.invoke({"assetGlossary": glossary, "conversationToDate": history})


def stream_rag_response(history: list, qte: str, knowledge: list, system_msg: str, user_msg: str, glossary: str):
    """Returns a synchronous generator of text tokens for SSE streaming."""
    cfg = get_config()["uc1"]
    llm = get_chat_llm(
        model_key="chat_deployment",
        temperature=cfg.get("rag_temperature", 0.6),
        max_tokens=cfg.get("rag_max_tokens", 2000),
    )
    prompt = ChatPromptTemplate.from_messages(
        [SystemMessagePromptTemplate.from_template(system_msg), user_msg]
    )
    chain = prompt | llm | StrOutputParser()
    return chain.stream(
        {"assetGlossary": glossary, "query": qte, "chatHistory": history, "knowledge": knowledge}
    )


# ── Public API ────────────────────────────────────────────────────────────────

def load_uc1_assets() -> dict:
    ap = get_config()["uc1"]["assets_path"]
    return {
        "qte_system": read_asset(ap, "QTESystemMessage"),
        "rag_system": read_asset(ap, "RAGSystemMessage"),
        "rag_user":   read_asset(ap, "RAGUserMessage"),
        "glossary":   read_asset(ap, "GSKGlossary"),
    }
