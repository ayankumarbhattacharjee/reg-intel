"""
UC2 – External RegIntel Risk Analyzer
Pipeline: Evaluate → Extract Insights → Compare to SOP → Risk Score
Yields SSE progress events as JSON-serialisable dicts.
"""
from __future__ import annotations

import queue
import threading
from enum import Enum
from typing import Generator

import pandas as pd
from langchain.output_parsers.enum import EnumOutputParser
from langchain_core.prompts.chat import ChatPromptTemplate, SystemMessagePromptTemplate
from pydantic import BaseModel, Field

from utils.llm_client import get_chat_llm, get_config, read_asset
from utils.pdf_utils import chunk_document, count_tokens, extract_text_from_pdf


TOPICS = [
    "Institutional Review Board/Independent Ethics Committee",
    "Investigator",
    "Sponsor",
    "Clinical Trial Protocol and protocol amendments",
    "Investigator's Brochure",
    "Conduct of Clinical Trial",
    "Monitoring",
    "Auditing",
    "Data handling and record keeping",
    "clinical trial reports",
    "Responsibilities of the Sponsor and Investigator",
    "Sponsor Inspection Preparation",
]


# ── Asset loader ──────────────────────────────────────────────────────────────

def _load(name: str) -> str:
    cfg = get_config()
    return read_asset(cfg["uc2"]["assets_path"], name)


# ── Stage 1 – Evaluation ──────────────────────────────────────────────────────

def _evaluate_chunk(chunk_text: str, topic: str) -> dict:
    class Evaluate(BaseModel):
        decision: bool = Field(description="True if the content relates to the topic.")
        justification: str = Field(description="Justification for the decision.")

    llm = get_chat_llm(model_key="mini_deployment", temperature=0).with_structured_output(Evaluate)
    prompt = ChatPromptTemplate.from_messages(
        [SystemMessagePromptTemplate.from_template(_load("eval_system_message")), _load("eval_user_message")]
    )
    result = (prompt | llm).invoke(
        {"chunk": chunk_text, "topic": topic, "GSKGlossary": _load("GSKGlossary")}
    )
    return {"decision": result.decision, "justification": result.justification}


def run_evaluation(df_chunks: pd.DataFrame, topic: str) -> tuple[pd.DataFrame, str, pd.Series]:
    decisions, justifications = [], []
    for _, row in df_chunks.iterrows():
        r = _evaluate_chunk(row["ChunkText"], topic)
        decisions.append("True" if r["decision"] else "False")
        justifications.append(r["justification"])

    df = df_chunks.copy()
    df.insert(0, "Decision", decisions)
    df.insert(1, "Justification", justifications)
    counts = df["Decision"].value_counts()
    consensus = counts.idxmax()
    return df, consensus, counts


# ── Stage 2 – Insight extraction ─────────────────────────────────────────────

def _classify_insight(insight: str) -> str:
    class InsightClass(Enum):
        IMPACT = "impact"
        CONSULTATION = "consultation"
        AWARENESS = "awareness"

    llm = get_chat_llm(temperature=0)
    parser = EnumOutputParser(enum=InsightClass)
    prompt = ChatPromptTemplate.from_messages(
        [
            SystemMessagePromptTemplate.from_template(_load("classification_system_message")),
            _load("classification_user_message"),
        ]
    ).partial(options=parser.get_format_instructions())
    result = (prompt | llm | parser).invoke({"insight": insight})
    return result.value


def _extract_insights_from_chunk(chunk_text: str, topic: str) -> pd.DataFrame:
    class Insights(BaseModel):
        completed: bool = Field(description="True when insight generation is complete.")
        insight: str = Field(description="A single MECE insight string.")

    glossary = _load("GSKGlossary")
    llm = get_chat_llm(temperature=0).with_structured_output(Insights)
    prompt = ChatPromptTemplate.from_messages(
        [SystemMessagePromptTemplate.from_template(_load("insight_system_message")), _load("insight_user_message")]
    )
    chain = prompt | llm

    new_insights: list[str] = []
    rows = []
    while True:
        counter = 5 - len(new_insights)
        resp = chain.invoke(
            {"chunk": chunk_text, "existing_insights": new_insights, "counter": counter,
             "GSKGlossary": glossary, "topic": topic}
        )
        classification = _classify_insight(resp.insight)
        new_insights.append(resp.insight)
        rows.append({"classification": classification, "insight": resp.insight, "chunk": chunk_text})
        if resp.completed and len(new_insights) >= 3:
            break
        if len(new_insights) == 5:
            break
    return pd.DataFrame(rows)


def run_insights(df_chunks: pd.DataFrame, topic: str) -> pd.DataFrame:
    frames = []
    for chunk_text in df_chunks["ChunkText"]:
        frames.append(_extract_insights_from_chunk(chunk_text, topic))
    return pd.concat(frames, ignore_index=True)


# ── Stage 3 – Compare to SOP ──────────────────────────────────────────────────

def run_compare(insight_df: pd.DataFrame, sop_df: pd.DataFrame, topic: str) -> pd.DataFrame:
    class Compare(BaseModel):
        review: bool = Field(description="True if a review is needed.")
        justification: str = Field(description="Why a review is needed.")

    glossary = _load("GSKGlossary")
    llm = get_chat_llm(temperature=0).with_structured_output(Compare)
    prompt = ChatPromptTemplate.from_messages(
        [SystemMessagePromptTemplate.from_template(_load("compare_system_message")), _load("compare_user_message")]
    )
    chain = prompt | llm

    rows = []
    for _, sop_row in sop_df.iterrows():
        for _, ins_row in insight_df.iterrows():
            resp = chain.invoke(
                {"sopChunk": sop_row["ChunkText"], "insight": ins_row["insight"],
                 "topic": topic, "GSKGlossary": glossary}
            )
            rows.append({
                "ReviewNeeded": resp.review,
                "Justification": resp.justification,
                "SOP": sop_row["ChunkText"],
                "Insight": ins_row["insight"],
            })
    return pd.DataFrame(rows)


# ── Stage 4 – Risk scoring ────────────────────────────────────────────────────

def run_risk_scoring(compare_df: pd.DataFrame, topic: str) -> pd.DataFrame:
    class RiskLevel(str, Enum):
        HIGH = "high"
        MEDIUM = "medium"
        LOW = "low"

    class Risk(BaseModel):
        risk_level: RiskLevel = Field(description="Risk classification.")
        justification: str = Field(description="Justification for the risk level.")
        advice: str = Field(description="Mitigation advice for the SOP.")

    llm = get_chat_llm(temperature=0).with_structured_output(Risk)
    prompt = ChatPromptTemplate.from_messages(
        [SystemMessagePromptTemplate.from_template(_load("risk_scoring_system_message")),
         _load("risk_scoring_user_message")]
    )
    chain = prompt | llm

    rows = []
    for _, row in compare_df.iterrows():
        resp = chain.invoke(
            {"comparison": row["Justification"], "insight": row["Insight"],
             "SOPchunk": row["SOP"], "topic": topic}
        )
        rows.append({
            "RiskLevel": resp.risk_level,
            "Justification": resp.justification,
            "Advice": resp.advice,
            "Insight": row["Insight"],
            "SOPChunk": row["SOP"],
        })
    return pd.DataFrame(rows)


# ── Orchestrator (streaming via SSE) ─────────────────────────────────────────

def run_pipeline(
    topic: str,
    ext_pdf_bytes: bytes,
    sop_pdf_bytes: bytes,
) -> Generator[dict, None, None]:
    """
    Generator that yields progress event dicts for SSE streaming.
    Each dict has keys: stage, message, data (optional).
    """
    cfg = get_config()
    chunk_size = cfg["uc2"].get("chunk_size", 10000)
    overlap = cfg["uc2"].get("chunk_overlap", 1000)

    event_q: queue.Queue = queue.Queue()
    error_holder: list = []

    def _run():
        try:
            # ── Extract & chunk ────────────────────────────────────────────
            event_q.put({"stage": "chunking", "message": "Extracting text from External Reg Intel PDF…"})
            ext_text, ext_pages = extract_text_from_pdf(ext_pdf_bytes)
            ext_tokens, _ = count_tokens(ext_text)

            event_q.put({"stage": "chunking", "message": "Extracting text from SOP PDF…"})
            sop_text, sop_pages = extract_text_from_pdf(sop_pdf_bytes)
            sop_tokens, _ = count_tokens(sop_text)

            event_q.put({
                "stage": "chunking",
                "message": "Documents extracted and tokenised.",
                "data": {"ext_tokens": ext_tokens, "sop_tokens": sop_tokens},
            })

            df_ext = chunk_document(ext_text, ext_pages, chunk_size, overlap)
            df_sop = chunk_document(sop_text, sop_pages, chunk_size, overlap)

            event_q.put({
                "stage": "chunking_done",
                "message": f"Chunked into {len(df_ext)} external and {len(df_sop)} SOP chunks.",
                "data": {
                    "ext_chunks": len(df_ext),
                    "sop_chunks": len(df_sop),
                    "ext_tokens": ext_tokens,
                    "sop_tokens": sop_tokens,
                },
            })

            # ── Evaluate ───────────────────────────────────────────────────
            event_q.put({"stage": "evaluating", "message": f"Evaluating relevance of {len(df_ext)} chunks for topic: {topic}…"})
            df_eval, consensus, counts = run_evaluation(df_ext, topic)

            eval_data = {
                "consensus": consensus,
                "counts": counts.to_dict(),
                "rows": df_eval[["Decision", "Justification", "StartPage", "EndPage"]].to_dict(orient="records"),
            }
            event_q.put({"stage": "evaluation_done", "message": f"Evaluation consensus: {consensus}", "data": eval_data})

            if consensus == "False":
                event_q.put({"stage": "aborted", "message": "Document not relevant to topic. Analysis stopped.", "data": None})
                event_q.put(None)
                return

            # ── Insights ───────────────────────────────────────────────────
            event_q.put({"stage": "insights", "message": "Extracting regulatory insights…"})
            df_insights = run_insights(df_ext, topic)
            impact_df = df_insights[df_insights["classification"] == "impact"].copy()

            insights_data = {
                "all": df_insights[["classification", "insight"]].to_dict(orient="records"),
                "impact_count": len(impact_df),
            }
            event_q.put({"stage": "insights_done", "message": f"Extracted {len(df_insights)} insights ({len(impact_df)} IMPACT).", "data": insights_data})

            if impact_df.empty:
                event_q.put({"stage": "aborted", "message": "No IMPACT insights found. Analysis stopped.", "data": None})
                event_q.put(None)
                return

            # ── Compare ────────────────────────────────────────────────────
            event_q.put({"stage": "comparing", "message": f"Comparing {len(impact_df)} impact insights against {len(df_sop)} SOP chunks…"})
            df_compare = run_compare(impact_df, df_sop, topic)
            needs_review = df_compare[df_compare["ReviewNeeded"] == True].copy()

            compare_data = {
                "total": len(df_compare),
                "needs_review": len(needs_review),
                "rows": df_compare[["ReviewNeeded", "Justification", "Insight"]].to_dict(orient="records"),
            }
            event_q.put({"stage": "compare_done", "message": f"Comparison done. {len(needs_review)} pairs require review.", "data": compare_data})

            if needs_review.empty:
                event_q.put({"stage": "aborted", "message": "No SOP reviews required. Analysis stopped.", "data": None})
                event_q.put(None)
                return

            # ── Risk scoring ───────────────────────────────────────────────
            event_q.put({"stage": "risk_scoring", "message": f"Risk scoring {len(needs_review)} comparison pairs…"})
            df_risks = run_risk_scoring(needs_review, topic)

            risk_data = {
                "rows": df_risks[["RiskLevel", "Justification", "Advice", "Insight"]].to_dict(orient="records"),
                "summary": df_risks["RiskLevel"].value_counts().to_dict(),
            }
            event_q.put({"stage": "complete", "message": "Risk analysis complete.", "data": risk_data})

        except Exception as exc:
            error_holder.append(exc)
            event_q.put({"stage": "error", "message": str(exc), "data": None})
        finally:
            event_q.put(None)  # sentinel

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    while True:
        event = event_q.get()
        if event is None:
            break
        yield event

    thread.join()
