"""
UC3 – External + Internal RegIntel Dual Analyzer
Pipeline: Parallel Evaluate → Dual Insights → Merge → Compare to SOP → Risk Score
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
    "Labelling",
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


def _load(name: str) -> str:
    cfg = get_config()
    return read_asset(cfg["uc3"]["assets_path"], name)


# ── Evaluation ────────────────────────────────────────────────────────────────

def _evaluate_chunk(chunk_text: str, topic: str, source: str) -> dict:
    class Evaluate(BaseModel):
        decision: bool = Field(description="True if content relates to the topic.")
        justification: str = Field(description="Justification.")

    sys_msg = _load("intl_eval_system_message" if source == "intl" else "ext_eval_system_message")
    user_msg = _load("intl_eval_user_message" if source == "intl" else "ext_eval_user_message")

    llm = get_chat_llm(temperature=0).with_structured_output(Evaluate)
    prompt = ChatPromptTemplate.from_messages(
        [SystemMessagePromptTemplate.from_template(sys_msg), user_msg]
    )
    result = (prompt | llm).invoke(
        {"chunk": chunk_text, "topic": topic, "GSKGlossary": _load("GSKGlossary")}
    )
    return {"decision": result.decision, "justification": result.justification}


def run_evaluation(df_chunks: pd.DataFrame, topic: str, source: str) -> tuple[pd.DataFrame, str, pd.Series]:
    decisions, justifications = [], []
    for _, row in df_chunks.iterrows():
        r = _evaluate_chunk(row["ChunkText"], topic, source)
        decisions.append("True" if r["decision"] else "False")
        justifications.append(r["justification"])
    df = df_chunks.copy()
    df.insert(0, "Decision", decisions)
    df.insert(1, "Justification", justifications)
    counts = df["Decision"].value_counts()
    return df, counts.idxmax(), counts


# ── Insights ──────────────────────────────────────────────────────────────────

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
    return (prompt | llm | parser).invoke({"insight": insight}).value


def _extract_insights_from_chunk(chunk_text: str, topic: str, source: str) -> pd.DataFrame:
    class Insights(BaseModel):
        completed: bool
        insight: str

    sys_msg = _load("intl_insight_system_message" if source == "intl" else "ext_insight_system_message")
    user_msg = _load("intl_insight_user_message" if source == "intl" else "ext_insight_user_message")
    glossary = _load("GSKGlossary")

    llm = get_chat_llm(temperature=0).with_structured_output(Insights)
    prompt = ChatPromptTemplate.from_messages(
        [SystemMessagePromptTemplate.from_template(sys_msg), user_msg]
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
        cls = _classify_insight(resp.insight)
        new_insights.append(resp.insight)
        rows.append({"classification": cls, "insight": resp.insight, "chunk": chunk_text})
        if resp.completed and len(new_insights) >= 3:
            break
        if len(new_insights) == 5:
            break
    return pd.DataFrame(rows)


def run_insights(df_chunks: pd.DataFrame, topic: str, source: str) -> pd.DataFrame:
    frames = [_extract_insights_from_chunk(ct, topic, source) for ct in df_chunks["ChunkText"]]
    return pd.concat(frames, ignore_index=True)


# ── Compare & Risk (reuse same logic as UC2) ──────────────────────────────────

def run_compare(insight_df: pd.DataFrame, sop_df: pd.DataFrame, topic: str) -> pd.DataFrame:
    class Compare(BaseModel):
        review: bool
        justification: str

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
                "Source": ins_row.get("source", ""),
            })
    return pd.DataFrame(rows)


def run_risk_scoring(compare_df: pd.DataFrame, topic: str) -> pd.DataFrame:
    class RiskLevel(str, Enum):
        HIGH = "high"
        MEDIUM = "medium"
        LOW = "low"

    class Risk(BaseModel):
        risk_level: RiskLevel
        justification: str
        advice: str

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
            "Source": row.get("Source", ""),
            "SOPChunk": row["SOP"],
        })
    return pd.DataFrame(rows)


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_pipeline(
    topic: str,
    ext_pdf_bytes: bytes,
    int_pdf_bytes: bytes,
    sop_pdf_bytes: bytes,
) -> Generator[dict, None, None]:
    cfg = get_config()
    chunk_size = cfg["uc3"].get("chunk_size", 10000)
    overlap = cfg["uc3"].get("chunk_overlap", 1000)

    event_q: queue.Queue = queue.Queue()

    def _run():
        try:
            # ── Extract & chunk ──────────────────────────────────────────
            event_q.put({"stage": "chunking", "message": "Extracting text from all PDFs…"})
            ext_text, ext_pages = extract_text_from_pdf(ext_pdf_bytes)
            int_text, int_pages = extract_text_from_pdf(int_pdf_bytes)
            sop_text, sop_pages = extract_text_from_pdf(sop_pdf_bytes)

            ext_tok, _ = count_tokens(ext_text)
            int_tok, _ = count_tokens(int_text)
            sop_tok, _ = count_tokens(sop_text)

            df_ext = chunk_document(ext_text, ext_pages, chunk_size, overlap)
            df_int = chunk_document(int_text, int_pages, chunk_size, overlap)
            df_sop = chunk_document(sop_text, sop_pages, chunk_size, overlap)

            event_q.put({
                "stage": "chunking_done",
                "message": f"Chunked: {len(df_ext)} external, {len(df_int)} internal, {len(df_sop)} SOP chunks.",
                "data": {
                    "ext_tokens": ext_tok, "int_tokens": int_tok, "sop_tokens": sop_tok,
                    "ext_chunks": len(df_ext), "int_chunks": len(df_int), "sop_chunks": len(df_sop),
                },
            })

            # ── Evaluate both sources ────────────────────────────────────
            event_q.put({"stage": "evaluating", "message": "Evaluating External Reg Intel relevance…"})
            df_ext_eval, ext_con, ext_counts = run_evaluation(df_ext, topic, "ext")

            event_q.put({"stage": "evaluating", "message": "Evaluating Internal Reg Intel relevance…"})
            df_int_eval, int_con, int_counts = run_evaluation(df_int, topic, "intl")

            eval_data = {
                "external": {"consensus": ext_con, "counts": ext_counts.to_dict()},
                "internal": {"consensus": int_con, "counts": int_counts.to_dict()},
            }
            event_q.put({
                "stage": "evaluation_done",
                "message": f"External: {ext_con} | Internal: {int_con}",
                "data": eval_data,
            })

            if ext_con == "False" and int_con == "False":
                event_q.put({"stage": "aborted", "message": "Both documents not relevant. Analysis stopped.", "data": None})
                event_q.put(None)
                return

            # ── Insights ─────────────────────────────────────────────────
            event_q.put({"stage": "insights", "message": "Extracting insights from External Reg Intel…"})
            df_ext_ins = run_insights(df_ext, topic, "ext")
            df_ext_ins["source"] = "external"

            event_q.put({"stage": "insights", "message": "Extracting insights from Internal Reg Intel…"})
            df_int_ins = run_insights(df_int, topic, "intl")
            df_int_ins["source"] = "internal"

            df_insights = pd.concat([df_ext_ins, df_int_ins], ignore_index=True)
            impact_df = df_insights[df_insights["classification"] == "impact"].copy()

            insights_data = {
                "all": df_insights[["classification", "insight", "source"]].to_dict(orient="records"),
                "impact_count": len(impact_df),
                "external_count": len(df_ext_ins),
                "internal_count": len(df_int_ins),
            }
            event_q.put({
                "stage": "insights_done",
                "message": f"{len(df_insights)} insights total ({len(impact_df)} IMPACT).",
                "data": insights_data,
            })

            if impact_df.empty:
                event_q.put({"stage": "aborted", "message": "No IMPACT insights found.", "data": None})
                event_q.put(None)
                return

            # ── Compare ──────────────────────────────────────────────────
            event_q.put({"stage": "comparing", "message": f"Comparing {len(impact_df)} insights vs {len(df_sop)} SOP chunks…"})
            df_compare = run_compare(impact_df, df_sop, topic)
            needs_review = df_compare[df_compare["ReviewNeeded"] == True].copy()

            event_q.put({
                "stage": "compare_done",
                "message": f"{len(needs_review)} pairs require review.",
                "data": {
                    "total": len(df_compare),
                    "needs_review": len(needs_review),
                    "rows": df_compare[["ReviewNeeded", "Justification", "Insight", "Source"]].to_dict(orient="records"),
                },
            })

            if needs_review.empty:
                event_q.put({"stage": "aborted", "message": "No SOP reviews required.", "data": None})
                event_q.put(None)
                return

            # ── Risk scoring ─────────────────────────────────────────────
            event_q.put({"stage": "risk_scoring", "message": f"Risk scoring {len(needs_review)} pairs…"})
            df_risks = run_risk_scoring(needs_review, topic)

            risk_data = {
                "rows": df_risks[["RiskLevel", "Justification", "Advice", "Insight", "Source"]].to_dict(orient="records"),
                "summary": df_risks["RiskLevel"].value_counts().to_dict(),
            }
            event_q.put({"stage": "complete", "message": "Dual-source risk analysis complete.", "data": risk_data})

        except Exception as exc:
            event_q.put({"stage": "error", "message": str(exc), "data": None})
        finally:
            event_q.put(None)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    while True:
        ev = event_q.get()
        if ev is None:
            break
        yield ev
    thread.join()
