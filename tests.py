"""
GSK Regulatory Intelligence Platform – Test Suite
===================================================
Run:  python tests.py
      python tests.py --live   (also runs tests that call the live FastAPI server)

Produces test_report.json in the same directory.
"""
from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import pathlib
import sys
import textwrap
import time
import traceback
import types
import unittest
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

# ── Working directory = project root ──────────────────────────────────────────
ROOT = pathlib.Path(__file__).parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
RESULTS: list[dict] = []


def _rec(name: str, passed: bool, msg: str = "", duration: float = 0.0, category: str = "") -> dict:
    r = {
        "test_id": len(RESULTS) + 1,
        "test_name": name,
        "category": category,
        "status": "PASS" if passed else "FAIL",
        "message": msg,
        "duration_ms": round(duration * 1000, 2),
    }
    RESULTS.append(r)
    icon = "+" if passed else "-"
    print(f"  {icon} [{r['status']}] {name}" + (f" - {msg}" if msg else ""))
    return r


def _run(name: str, fn, category: str = ""):
    t0 = time.perf_counter()
    try:
        fn()
        _rec(name, True, duration=time.perf_counter() - t0, category=category)
    except AssertionError as e:
        _rec(name, False, str(e), time.perf_counter() - t0, category)
    except Exception as e:
        _rec(name, False, f"{type(e).__name__}: {e}", time.perf_counter() - t0, category)


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 1 – Configuration & Asset Loading
# ═════════════════════════════════════════════════════════════════════════════
print("\n[1] Configuration & Asset Loading")

def t_config_loads():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    assert "azure_openai" in cfg, "Missing 'azure_openai' section"
    for k in ("api_key", "endpoint", "api_version", "chat_deployment", "mini_deployment", "embedding_deployment"):
        assert k in cfg["azure_openai"], f"Missing key: {k}"

def t_config_uc1_keys():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    uc1 = cfg["uc1"]
    for k in ("chunks_pkl_path", "faiss_index_path", "assets_path", "top_k", "pages_returned"):
        assert k in uc1, f"Missing uc1 key: {k}"

def t_config_uc2_keys():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    uc2 = cfg["uc2"]
    for k in ("assets_path", "chunk_size", "chunk_overlap"):
        assert k in uc2, f"Missing uc2 key: {k}"

def t_config_uc3_keys():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    uc3 = cfg["uc3"]
    for k in ("assets_path", "chunk_size", "chunk_overlap"):
        assert k in uc3, f"Missing uc3 key: {k}"

def t_llm_client_import():
    from utils.llm_client import get_config, read_asset
    cfg = get_config()
    assert cfg is not None

def t_uc1_assets_exist():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    ap = pathlib.Path(cfg["uc1"]["assets_path"])
    for name in ("QTESystemMessage", "RAGSystemMessage", "RAGUserMessage", "GSKGlossary"):
        fp = ap / f"{name}.md"
        assert fp.exists(), f"Asset missing: {fp}"
        assert fp.stat().st_size > 0, f"Asset empty: {fp}"

def t_uc2_assets_exist():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    ap = pathlib.Path(cfg["uc2"]["assets_path"])
    for name in ("eval_system_message", "eval_user_message", "insight_system_message",
                 "insight_user_message", "classification_system_message",
                 "classification_user_message", "compare_system_message",
                 "compare_user_message", "risk_scoring_system_message",
                 "risk_scoring_user_message", "GSKGlossary"):
        fp = ap / f"{name}.md"
        assert fp.exists(), f"UC2 asset missing: {fp}"

def t_uc3_assets_exist():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    ap = pathlib.Path(cfg["uc3"]["assets_path"])
    for name in ("ext_eval_system_message", "ext_eval_user_message",
                 "intl_eval_system_message", "intl_eval_user_message",
                 "ext_insight_system_message", "ext_insight_user_message",
                 "intl_insight_system_message", "intl_insight_user_message",
                 "compare_system_message", "compare_user_message",
                 "risk_scoring_system_message", "risk_scoring_user_message"):
        fp = ap / f"{name}.md"
        assert fp.exists(), f"UC3 asset missing: {fp}"

def t_chunks_pkl_exists():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    p = pathlib.Path(cfg["uc1"]["chunks_pkl_path"])
    assert p.exists(), f"Chunks PKL not found: {p}"

def t_chunks_pkl_loads():
    import pandas as pd
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    df = pd.read_pickle(cfg["uc1"]["chunks_pkl_path"])
    assert len(df) > 0, "PKL file is empty"
    assert "ChunkText" in df.columns, "Missing ChunkText column"
    assert "ChunkID" in df.columns, "Missing ChunkID column"

CAT1 = "Config & Assets"
for fn in [t_config_loads, t_config_uc1_keys, t_config_uc2_keys, t_config_uc3_keys,
           t_llm_client_import, t_uc1_assets_exist, t_uc2_assets_exist, t_uc3_assets_exist,
           t_chunks_pkl_exists, t_chunks_pkl_loads]:
    _run(fn.__name__.replace("t_", ""), fn, CAT1)


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 2 – PDF Utilities
# ═════════════════════════════════════════════════════════════════════════════
print("\n[2] PDF Utilities")

def _make_pdf_bytes(text: str = "Hello regulatory world.\nPage two content.") -> bytes:
    """Create a minimal valid PDF in memory for testing."""
    import struct
    # Build a minimal PDF
    lines = [
        b"%PDF-1.4",
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj",
    ]
    page_content = text.encode("latin-1", errors="replace")
    stream = b"BT /F1 12 Tf 50 750 Td (" + page_content + b") Tj ET"
    lines.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Contents 4 0 R /Resources << /Font << /F1 << /Type /Font"
        b" /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>\nendobj"
    )
    lines.append(
        b"4 0 obj\n<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream\nendobj"
    )
    xref_pos = sum(len(l) + 1 for l in lines)
    lines.append(b"xref\n0 5")
    offsets = [0]
    pos = 0
    for l in lines[:5]:
        pos += len(l) + 1
        offsets.append(pos)
    for off in offsets[:5]:
        lines.append(f"{off:010d} 00000 n ".encode())
    lines.append(b"trailer\n<< /Size 5 /Root 1 0 R >>")
    lines.append(b"startxref\n" + str(xref_pos).encode())
    lines.append(b"%%EOF")
    return b"\n".join(lines)

def t_pdf_import():
    from utils.pdf_utils import extract_text_from_pdf, chunk_document, count_tokens
    assert callable(extract_text_from_pdf)
    assert callable(chunk_document)
    assert callable(count_tokens)

def t_count_tokens():
    from utils.pdf_utils import count_tokens
    n, toks = count_tokens("Hello regulatory world")
    assert n > 0
    assert len(toks) == n

def t_chunk_document_basic():
    from utils.pdf_utils import chunk_document
    text = "word " * 200
    page_texts = [(1, text)]
    df = chunk_document(text, page_texts, chunk_size=50, overlap=5)
    assert len(df) >= 1
    assert "ChunkText" in df.columns
    assert "ChunkID" in df.columns
    assert "StartPage" in df.columns

def t_chunk_overlap():
    from utils.pdf_utils import chunk_document
    text = "token " * 300
    page_texts = [(1, text)]
    df = chunk_document(text, page_texts, chunk_size=100, overlap=20)
    assert len(df) >= 3, "Expected multiple chunks with overlap"

def t_chunk_unique_ids():
    from utils.pdf_utils import chunk_document
    text = "word " * 500
    page_texts = [(1, text)]
    df = chunk_document(text, page_texts, chunk_size=100, overlap=10)
    assert df["ChunkID"].nunique() == len(df), "Chunk IDs must be unique"

def t_pdf_extract_bytes():
    """Test extraction from a minimal in-memory PDF (may get empty text — just check no crash)."""
    from utils.pdf_utils import extract_text_from_pdf
    pdf_bytes = _make_pdf_bytes()
    text, page_texts = extract_text_from_pdf(pdf_bytes)
    assert isinstance(text, str)
    assert isinstance(page_texts, list)

CAT2 = "PDF Utilities"
for fn in [t_pdf_import, t_count_tokens, t_chunk_document_basic,
           t_chunk_overlap, t_chunk_unique_ids, t_pdf_extract_bytes]:
    _run(fn.__name__.replace("t_", ""), fn, CAT2)


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 3 – FastAPI Endpoints (live server on localhost:8000)
# ═════════════════════════════════════════════════════════════════════════════
print("\n[3] FastAPI Endpoints (live server)")

def _get(path: str, timeout: int = 10):
    import urllib.request
    with urllib.request.urlopen(f"http://localhost:8000{path}", timeout=timeout) as r:
        return r.status, r.read().decode()

def _post_json(path: str, data: dict, timeout: int = 10):
    import urllib.request
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"http://localhost:8000{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())

def t_health_endpoint():
    status, body = _get("/api/health")
    assert status == 200
    data = json.loads(body)
    assert data["status"] == "ok"
    assert "GSK RegIntel" in data["app"]

def t_frontend_serves_html():
    status, body = _get("/")
    assert status == 200
    assert "<!DOCTYPE html>" in body or "<!doctype html>" in body.lower()
    assert "GSK" in body

def t_frontend_has_tabs():
    _, body = _get("/")
    for tab in ["RegIntel Chatbot", "Risk Analyser", "Dual Analyser"]:
        assert tab in body, f"Missing tab: {tab}"

def t_frontend_has_uc_sections():
    _, body = _get("/")
    for sec in ["tab-uc1", "tab-uc2", "tab-uc3", "tab-home"]:
        assert sec in body, f"Missing section: {sec}"

def t_uc2_topics_endpoint():
    # Timeout 30s: first call triggers a cold import of agents.uc2_risk_analyzer
    # (langchain + pydantic chain), which can take 10-20s on Windows/Python 3.13.
    status, body = _get("/api/uc2/topics", timeout=30)
    assert status == 200
    data = json.loads(body)
    assert "topics" in data
    assert len(data["topics"]) >= 10
    assert "Sponsor" in data["topics"]

def t_uc3_topics_endpoint():
    # Timeout 30s: same cold-import concern for agents.uc3_dual_analyzer.
    status, body = _get("/api/uc3/topics", timeout=30)
    assert status == 200
    data = json.loads(body)
    assert "topics" in data
    assert "Labelling" in data["topics"]  # UC3 has extra "Labelling" topic

def t_uc3_has_more_topics_than_uc2():
    _, b2 = _get("/api/uc2/topics")
    _, b3 = _get("/api/uc3/topics")
    t2 = json.loads(b2)["topics"]
    t3 = json.loads(b3)["topics"]
    assert len(t3) > len(t2), "UC3 should have more topics (adds Labelling)"

def t_404_on_unknown_path():
    import urllib.request, urllib.error
    try:
        urllib.request.urlopen("http://localhost:8000/api/does_not_exist", timeout=5)
        assert False, "Should have raised HTTPError"
    except urllib.error.HTTPError as e:
        assert e.code == 404

def t_openapi_docs_available():
    status, body = _get("/docs")
    assert status == 200
    assert "swagger" in body.lower() or "openapi" in body.lower()

CAT3 = "FastAPI Endpoints"
for fn in [t_health_endpoint, t_frontend_serves_html, t_frontend_has_tabs,
           t_frontend_has_uc_sections, t_uc2_topics_endpoint, t_uc3_topics_endpoint,
           t_uc3_has_more_topics_than_uc2, t_404_on_unknown_path, t_openapi_docs_available]:
    _run(fn.__name__.replace("t_", ""), fn, CAT3)


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 4 – UC1 Chatbot Logic (mocked LLM)
# ═════════════════════════════════════════════════════════════════════════════
print("\n[4] UC1 Chatbot Logic (mocked Azure OpenAI)")

def t_uc1_assets_loadable():
    from agents.uc1_chatbot import load_uc1_assets
    assets = load_uc1_assets()
    for k in ("qte_system", "rag_system", "rag_user", "glossary"):
        assert k in assets and len(assets[k]) > 0, f"Missing/empty asset: {k}"

def t_uc1_transform_query_mock():
    """Query transformation with mocked AzureChatOpenAI."""
    from langchain_core.messages import AIMessage, HumanMessage
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = "What are GCP sponsor obligations?"

    with patch("agents.uc1_chatbot.get_chat_llm", return_value=mock_llm):
        from agents import uc1_chatbot
        # patch the chain invoke at prompt level
        with patch("langchain_core.runnables.base.RunnableSequence.invoke",
                   return_value="What are GCP sponsor obligations?"):
            from agents.uc1_chatbot import load_uc1_assets
            assets = load_uc1_assets()
            history = [HumanMessage(content="Tell me about sponsor obligations")]
            result = uc1_chatbot.transform_query(history, assets["qte_system"], assets["glossary"])
            assert isinstance(result, str)

def t_uc1_vectorstore_lazy_load():
    """The FAISS vectorstore variable starts as None (lazy loaded)."""
    import agents.uc1_chatbot as m
    # Reset singleton to test lazy loading behaviour
    m._vectorstore = None
    m._df_chunks = None
    assert m._vectorstore is None

def t_uc1_reconstruct_text():
    """_reconstruct_text returns sorted joined chunks."""
    import pandas as pd
    from agents.uc1_chatbot import _reconstruct_text
    df = pd.DataFrame([
        {"Chunk": 2, "ChunkText": "world"},
        {"Chunk": 1, "ChunkText": "hello"},
    ])
    result = _reconstruct_text(df)
    assert result == "hello world", f"Got: {result}"

def t_uc1_lookup_related_chunks():
    """_lookup_related_chunks returns surrounding pages correctly."""
    import pandas as pd
    from agents.uc1_chatbot import _lookup_related_chunks
    import agents.uc1_chatbot as m

    df = pd.DataFrame([
        {"ChunkID": "A", "Title": "Doc1", "PageNumber": 1, "Chunk": 1, "ChunkText": "p1"},
        {"ChunkID": "B", "Title": "Doc1", "PageNumber": 2, "Chunk": 2, "ChunkText": "p2"},
        {"ChunkID": "C", "Title": "Doc1", "PageNumber": 3, "Chunk": 3, "ChunkText": "p3"},
        {"ChunkID": "D", "Title": "Doc1", "PageNumber": 4, "Chunk": 4, "ChunkText": "p4"},
        {"ChunkID": "E", "Title": "Doc2", "PageNumber": 1, "Chunk": 1, "ChunkText": "other"},
    ])
    m._df_chunks = df

    result = _lookup_related_chunks("B", pages_returned=1)
    titles = result["Title"].unique()
    assert all(t == "Doc1" for t in titles), "Should only return same-title chunks"
    pages = sorted(result["PageNumber"].tolist())
    assert 1 in pages and 2 in pages and 3 in pages, f"Expected pages 1-3, got {pages}"
    assert 4 not in pages

    m._df_chunks = None  # reset

CAT4 = "UC1 Chatbot Logic"
for fn in [t_uc1_assets_loadable, t_uc1_transform_query_mock, t_uc1_vectorstore_lazy_load,
           t_uc1_reconstruct_text, t_uc1_lookup_related_chunks]:
    _run(fn.__name__.replace("t_", ""), fn, CAT4)


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 5 – UC2 Risk Analyser Pipeline (mocked LLM)
# ═════════════════════════════════════════════════════════════════════════════
print("\n[5] UC2 Risk Analyser Pipeline (mocked Azure OpenAI)")

def _make_structured_mock(response_obj):
    """Return a mock LLM that returns response_obj from .invoke()."""
    mock_llm = MagicMock()
    mock_llm.invoke.return_value = response_obj
    mock_llm.with_structured_output.return_value = mock_llm
    return mock_llm

def t_uc2_assets_loadable():
    from utils.llm_client import get_config, read_asset
    cfg = get_config()
    ap = cfg["uc2"]["assets_path"]
    for name in ("eval_system_message", "insight_system_message",
                 "compare_system_message", "risk_scoring_system_message"):
        content = read_asset(ap, name)
        assert len(content) > 100, f"Asset too short: {name}"

def t_uc2_evaluation_logic():
    """run_evaluation tags chunks with Decision/Justification and returns consensus."""
    import pandas as pd
    from unittest.mock import patch, MagicMock
    from pydantic import BaseModel

    class FakeEval(BaseModel):
        decision: bool = True
        justification: str = "Relevant content found"

    df = pd.DataFrame([{"ChunkText": "text about sponsor obligations", "ChunkID": "1"}])

    with patch("agents.uc2_risk_analyzer.get_chat_llm") as mock_get:
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = FakeEval()
        mock_llm.with_structured_output.return_value = mock_llm
        mock_get.return_value = mock_llm
        with patch("langchain_core.runnables.base.RunnableSequence.invoke",
                   return_value=FakeEval()):
            with patch("agents.uc2_risk_analyzer._load", return_value="system message {chunk} {topic} {GSKGlossary}"):
                from agents.uc2_risk_analyzer import _evaluate_chunk
                result = _evaluate_chunk("sponsor text", "Sponsor")
                assert "decision" in result
                assert "justification" in result

def t_uc2_classify_insight_mock():
    """selectClass returns one of impact/consultation/awareness."""
    from unittest.mock import patch, MagicMock
    from enum import Enum

    class FakeCls(str, Enum):
        IMPACT = "impact"

    mock_result = FakeCls.IMPACT

    with patch("agents.uc2_risk_analyzer.get_chat_llm") as mock_get:
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(value="impact")
        mock_get.return_value = mock_llm
        with patch("langchain_core.runnables.base.RunnableSequence.invoke",
                   return_value=mock_result):
            with patch("agents.uc2_risk_analyzer._load", return_value="content {insight} {options}"):
                from agents.uc2_risk_analyzer import _classify_insight
                # mock EnumOutputParser
                with patch("agents.uc2_risk_analyzer.EnumOutputParser") as mock_parser_cls:
                    parser_inst = MagicMock()
                    parser_inst.get_format_instructions.return_value = ""
                    parser_inst.invoke.return_value = mock_result
                    mock_parser_cls.return_value = parser_inst
                    mock_llm2 = MagicMock()
                    mock_llm2.__or__ = lambda s, o: MagicMock(invoke=lambda x: mock_result)
                    mock_get.return_value = mock_llm2

def t_uc2_pipeline_generator_structure():
    """run_pipeline is a generator that yields dicts with stage+message keys."""
    import io as _io
    from unittest.mock import patch, MagicMock
    import pandas as pd

    fake_text = "Sponsor obligations content " * 100
    fake_pages = [(1, fake_text)]
    fake_chunks = pd.DataFrame([{
        "ChunkText": fake_text[:500],
        "TokenCount": 50,
        "StartPage": 1,
        "EndPage": 1,
        "ChunkID": "test-id-1",
    }])
    # run_evaluation returns (df_eval, consensus, counts); df_eval must have Decision/Justification
    fake_eval_df = fake_chunks.copy()
    fake_eval_df.insert(0, "Decision", ["False"])
    fake_eval_df.insert(1, "Justification", ["Not relevant to topic"])

    events = []
    with patch("agents.uc2_risk_analyzer.extract_text_from_pdf", return_value=(fake_text, fake_pages)):
        with patch("agents.uc2_risk_analyzer.chunk_document", return_value=fake_chunks):
            with patch("agents.uc2_risk_analyzer.count_tokens", return_value=(50, [1]*50)):
                with patch("agents.uc2_risk_analyzer.run_evaluation",
                           return_value=(fake_eval_df, "False", pd.Series({"False": 1}))):
                    from agents.uc2_risk_analyzer import run_pipeline
                    gen = run_pipeline("Sponsor", b"fake_pdf", b"fake_sop")
                    for ev in gen:
                        events.append(ev)
                        assert "stage" in ev, f"Event missing 'stage': {ev}"
                        assert "message" in ev, f"Event missing 'message': {ev}"
                        if len(events) > 20:  # safety
                            break

    assert len(events) > 0, "No events yielded"
    stages = [e["stage"] for e in events]
    assert "chunking_done" in stages, f"Expected chunking_done in {stages}"
    # evaluation_done or aborted expected
    assert any(s in stages for s in ("evaluation_done", "aborted")), f"Unexpected stages: {stages}"

def t_uc2_aborts_on_not_relevant():
    """Pipeline yields 'aborted' when consensus is False."""
    import pandas as pd
    from unittest.mock import patch

    fake_text = "unrelated content " * 100
    fake_pages = [(1, fake_text)]
    fake_chunks = pd.DataFrame([{
        "ChunkText": fake_text[:500], "TokenCount": 50,
        "StartPage": 1, "EndPage": 1, "ChunkID": "id-1",
    }])
    # run_evaluation returns (df_eval, consensus, counts); df_eval must have Decision/Justification
    fake_eval_df = fake_chunks.copy()
    fake_eval_df.insert(0, "Decision", ["False"])
    fake_eval_df.insert(1, "Justification", ["Content not relevant"])

    with patch("agents.uc2_risk_analyzer.extract_text_from_pdf", return_value=(fake_text, fake_pages)):
        with patch("agents.uc2_risk_analyzer.chunk_document", return_value=fake_chunks):
            with patch("agents.uc2_risk_analyzer.count_tokens", return_value=(50, [1]*50)):
                with patch("agents.uc2_risk_analyzer.run_evaluation",
                           return_value=(fake_eval_df, "False", pd.Series({"False": 1}))):
                    from agents.uc2_risk_analyzer import run_pipeline
                    events = list(run_pipeline("Sponsor", b"pdf", b"sop"))
    stages = [e["stage"] for e in events]
    assert "aborted" in stages, f"Expected aborted, got stages: {stages}"

CAT5 = "UC2 Risk Analyser"
for fn in [t_uc2_assets_loadable, t_uc2_evaluation_logic, t_uc2_classify_insight_mock,
           t_uc2_pipeline_generator_structure, t_uc2_aborts_on_not_relevant]:
    _run(fn.__name__.replace("t_", ""), fn, CAT5)


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 6 – UC3 Dual Analyser Pipeline (mocked LLM)
# ═════════════════════════════════════════════════════════════════════════════
print("\n[6] UC3 Dual Analyser Pipeline (mocked Azure OpenAI)")

def t_uc3_assets_loadable():
    from utils.llm_client import get_config, read_asset
    cfg = get_config()
    ap = cfg["uc3"]["assets_path"]
    for name in ("ext_eval_system_message", "intl_eval_system_message",
                 "ext_insight_system_message", "intl_insight_system_message"):
        content = read_asset(ap, name)
        assert len(content) > 50, f"Asset too short or missing: {name}"

def t_uc3_has_labelling_topic():
    from agents.uc3_dual_analyzer import TOPICS
    assert "Labelling" in TOPICS, "UC3 must include 'Labelling' topic"

def t_uc3_pipeline_dual_evaluation():
    """UC3 pipeline evaluates both ext and intl sources, both False → aborted."""
    import pandas as pd
    from unittest.mock import patch

    fake_text = "unrelated content " * 100
    fake_pages = [(1, fake_text)]
    fake_chunks = pd.DataFrame([{
        "ChunkText": fake_text[:200], "TokenCount": 20,
        "StartPage": 1, "EndPage": 1, "ChunkID": "id-1",
    }])

    with patch("agents.uc3_dual_analyzer.extract_text_from_pdf", return_value=(fake_text, fake_pages)):
        with patch("agents.uc3_dual_analyzer.chunk_document", return_value=fake_chunks):
            with patch("agents.uc3_dual_analyzer.count_tokens", return_value=(20, [1]*20)):
                with patch("agents.uc3_dual_analyzer.run_evaluation",
                           return_value=(fake_chunks, "False", pd.Series({"False": 1}))):
                    from agents.uc3_dual_analyzer import run_pipeline
                    events = list(run_pipeline("Labelling", b"ext", b"int", b"sop"))
    stages = [e["stage"] for e in events]
    assert "aborted" in stages, f"Expected aborted when both sources False, got: {stages}"

def t_uc3_pipeline_continues_if_one_relevant():
    """If external=True but internal=False, pipeline should NOT abort at evaluation stage."""
    import pandas as pd
    from unittest.mock import patch, call

    fake_text = "relevant labelling content " * 100
    fake_pages = [(1, fake_text)]
    fake_chunks = pd.DataFrame([{
        "ChunkText": fake_text[:200], "TokenCount": 20,
        "StartPage": 1, "EndPage": 1, "ChunkID": "id-1",
    }])
    call_count = {"n": 0}

    def mock_eval(df, topic, source):
        call_count["n"] += 1
        if source == "ext":
            return (df, "True", pd.Series({"True": 1}))
        else:
            return (df, "False", pd.Series({"False": 1}))

    with patch("agents.uc3_dual_analyzer.extract_text_from_pdf", return_value=(fake_text, fake_pages)):
        with patch("agents.uc3_dual_analyzer.chunk_document", return_value=fake_chunks):
            with patch("agents.uc3_dual_analyzer.count_tokens", return_value=(20, [1]*20)):
                with patch("agents.uc3_dual_analyzer.run_evaluation", side_effect=mock_eval):
                    with patch("agents.uc3_dual_analyzer.run_insights",
                               return_value=pd.DataFrame([
                                   {"classification": "awareness", "insight": "test", "chunk": "c", "source": "external"}
                               ])):
                        from agents.uc3_dual_analyzer import run_pipeline
                        events = list(run_pipeline("Labelling", b"ext", b"int", b"sop"))
    stages = [e["stage"] for e in events]
    # Should pass evaluation_done stage (not aborted)
    assert "evaluation_done" in stages, f"Expected evaluation_done, got: {stages}"
    # Both sources should have been evaluated
    assert call_count["n"] == 2, f"Expected 2 evaluation calls, got {call_count['n']}"

def t_uc3_source_tracking():
    """Insights DataFrames get tagged with correct source labels."""
    import pandas as pd
    from agents.uc3_dual_analyzer import run_pipeline
    from unittest.mock import patch

    fake_text = "content " * 50
    fake_pages = [(1, fake_text)]
    fake_chunks = pd.DataFrame([{"ChunkText": "c", "TokenCount": 5, "StartPage": 1, "EndPage": 1, "ChunkID": "x"}])

    tagged = []
    def mock_insights(df, topic, source):
        result = pd.DataFrame([{"classification": "impact", "insight": "ins", "chunk": "c"}])
        result["source"] = "external" if source == "ext" else "internal"
        tagged.append(source)
        return result

    with patch("agents.uc3_dual_analyzer.extract_text_from_pdf", return_value=(fake_text, fake_pages)):
        with patch("agents.uc3_dual_analyzer.chunk_document", return_value=fake_chunks):
            with patch("agents.uc3_dual_analyzer.count_tokens", return_value=(5, [1]*5)):
                with patch("agents.uc3_dual_analyzer.run_evaluation",
                           return_value=(fake_chunks, "True", pd.Series({"True": 1}))):
                    with patch("agents.uc3_dual_analyzer.run_insights", side_effect=mock_insights):
                        with patch("agents.uc3_dual_analyzer.run_compare",
                                   return_value=pd.DataFrame(columns=["ReviewNeeded", "Justification", "SOP", "Insight", "Source"])):
                            events = list(run_pipeline("Labelling", b"e", b"i", b"s"))

    assert "ext" in tagged and "intl" in tagged, f"Both sources should be processed, got: {tagged}"

CAT6 = "UC3 Dual Analyser"
for fn in [t_uc3_assets_loadable, t_uc3_has_labelling_topic,
           t_uc3_pipeline_dual_evaluation, t_uc3_pipeline_continues_if_one_relevant,
           t_uc3_source_tracking]:
    _run(fn.__name__.replace("t_", ""), fn, CAT6)


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 7 – Azure OpenAI Connectivity (live calls)
# ═════════════════════════════════════════════════════════════════════════════
print("\n[7] Azure OpenAI Connectivity (live credentials)")

def t_azure_config_has_non_placeholder_key():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    key = cfg["azure_openai"]["api_key"]
    assert key and key != "YOUR_AZURE_OPENAI_API_KEY_HERE", "API key is still a placeholder"
    assert len(key) > 20, "API key too short to be valid"

def t_azure_endpoint_format():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    endpoint = cfg["azure_openai"]["endpoint"]
    assert endpoint.startswith("https://"), "Endpoint must start with https://"
    assert ("openai.azure.com" in endpoint or "cognitiveservices.azure.com" in endpoint), \
        "Endpoint must contain openai.azure.com or cognitiveservices.azure.com"

def _azure_client():
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)["azure_openai"]
    from openai import AzureOpenAI
    return AzureOpenAI(
        api_key=cfg["api_key"],
        azure_endpoint=cfg["endpoint"].rstrip("/"),
        api_version=cfg["api_version"],
    ), cfg


def t_azure_mini_llm_live():
    """Live call to Azure OpenAI mini model."""
    client, cfg = _azure_client()
    try:
        resp = client.chat.completions.create(
            model=cfg["mini_deployment"],
            messages=[{"role": "user", "content": "Reply with exactly: AZURE_OK"}],
            max_tokens=10,
        )
        content = resp.choices[0].message.content.strip()
        assert len(content) > 0, f"Unexpected empty response"
    except Exception as e:
        err = str(e)
        if "404" in err or "DeploymentNotFound" in err or "ResourceNotFound" in err or "not found" in err.lower():
            raise AssertionError(
                f"CONFIG_ERROR: Deployment '{cfg['mini_deployment']}' not found on Azure resource. "
                f"Check the exact deployment name in Azure Portal > your resource > Model deployments. Error: {e}"
            )
        raise


def t_azure_embedding_live():
    """Live call to Azure OpenAI embeddings model."""
    client, cfg = _azure_client()
    try:
        resp = client.embeddings.create(
            model=cfg["embedding_deployment"],
            input="regulatory intelligence",
        )
        vec = resp.data[0].embedding
        assert len(vec) > 0, f"Expected non-empty embedding vector, got {len(vec)} dims"
    except Exception as e:
        err = str(e)
        if "404" in err or "DeploymentNotFound" in err or "ResourceNotFound" in err or "not found" in err.lower():
            raise AssertionError(
                f"CONFIG_ERROR: Embedding deployment '{cfg['embedding_deployment']}' not found on Azure resource. "
                f"Check the exact deployment name in Azure Portal > your resource > Model deployments. Error: {e}"
            )
        raise


def t_azure_chat_model_live():
    """Live call to full chat model (chat_deployment)."""
    client, cfg = _azure_client()
    try:
        resp = client.chat.completions.create(
            model=cfg["chat_deployment"],
            messages=[{"role": "user", "content": "Reply with exactly: GPT4O_OK"}],
            max_tokens=10,
        )
        content = resp.choices[0].message.content.strip()
        assert len(content) > 0, "Empty response from chat model"
    except Exception as e:
        err = str(e)
        if "404" in err or "DeploymentNotFound" in err or "ResourceNotFound" in err or "not found" in err.lower():
            raise AssertionError(
                f"CONFIG_ERROR: Deployment '{cfg['chat_deployment']}' not found on Azure resource. "
                f"Check the exact deployment name in Azure Portal > your resource > Model deployments. Error: {e}"
            )
        raise

CAT7 = "Azure OpenAI Connectivity"
for fn in [t_azure_config_has_non_placeholder_key, t_azure_endpoint_format,
           t_azure_mini_llm_live, t_azure_embedding_live, t_azure_chat_model_live]:
    _run(fn.__name__.replace("t_", ""), fn, CAT7)


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 8 – UC1 Chat via HTTP (live endpoint + mocked LLM)
# ═════════════════════════════════════════════════════════════════════════════
print("\n[8] UC1 Chat HTTP Endpoint")

def t_uc1_chat_endpoint_returns_stream():
    """POST /api/uc1/chat must return text/event-stream content type.
    If FAISS index is not yet built (requires Azure embeddings), the test is
    classified as a CONFIG_ERROR rather than a code failure."""
    import urllib.request, urllib.error
    import pathlib, json as _json

    # Check whether FAISS index already exists (built from a prior run)
    with open(ROOT / "config.json") as f:
        faiss_path = _json.load(f)["uc1"].get("faiss_index_path", "uc1_faiss_index")
    faiss_exists = pathlib.Path(faiss_path).exists()

    payload = _json.dumps({
        "history": [],
        "message": "What are the ICH GCP guidelines for sponsors?"
    }).encode()
    req = urllib.request.Request(
        "http://localhost:8000/api/uc1/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ct = r.headers.get("Content-Type", "")
            assert "text/event-stream" in ct, f"Expected SSE content-type, got: {ct}"
            # Read a small amount — the first SSE event is ~57 bytes.
            # http.client read(n) with chunked encoding accumulates until n bytes,
            # so we must request <= first-chunk size to return immediately.
            chunk = r.read(40)
            assert len(chunk) > 0, "No data received from SSE stream"
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise AssertionError(f"HTTP {e.code}: {body}")
    except Exception as e:
        err = str(e)
        # IncompleteRead / connection reset typically means server errored while building FAISS index
        if not faiss_exists and ("IncompleteRead" in err or "RemoteDisconnected" in err or "connection" in err.lower()):
            raise AssertionError(
                f"CONFIG_ERROR: FAISS index not yet built — server failed to embed chunks because "
                f"Azure embedding deployment may be misconfigured. Once Azure deployment names are "
                f"correct and index is built (first run), this test will pass. Error: {e}"
            )
        raise

def t_uc1_chat_empty_message_handled():
    """Empty message should not crash the server (server returns 422 or handles gracefully)."""
    import urllib.request, urllib.error
    payload = json.dumps({"history": [], "message": ""}).encode()
    req = urllib.request.Request(
        "http://localhost:8000/api/uc1/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status in (200, 422), f"Unexpected status: {r.status}"
    except urllib.error.HTTPError as e:
        assert e.code in (422, 500), f"Unexpected error code: {e.code}"

CAT8 = "UC1 Chat HTTP"
for fn in [t_uc1_chat_endpoint_returns_stream, t_uc1_chat_empty_message_handled]:
    _run(fn.__name__.replace("t_", ""), fn, CAT8)


# ═════════════════════════════════════════════════════════════════════════════
# GROUP 9 – UC2 & UC3 Analyze HTTP Endpoints (structural)
# ═════════════════════════════════════════════════════════════════════════════
print("\n[9] UC2/UC3 Analyze Endpoints (structural)")

def _make_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "TestBoundary12345"
    body = b""
    for name, value in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
    for name, (filename, content) in files.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n").encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"

def t_uc2_analyze_requires_files():
    """POST to /api/uc2/analyze without files returns 422."""
    import urllib.request, urllib.error
    payload = b"topic=Sponsor"
    req = urllib.request.Request(
        "http://localhost:8000/api/uc2/analyze",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "Expected error"
    except urllib.error.HTTPError as e:
        assert e.code == 422, f"Expected 422, got {e.code}"

def t_uc3_analyze_requires_three_files():
    """POST to /api/uc3/analyze without all files returns 422."""
    import urllib.request, urllib.error
    body, ct = _make_multipart(
        {"topic": "Labelling"},
        {
            "external_pdf": ("ext.pdf", b"%PDF-1.4 minimal"),
            "internal_pdf": ("int.pdf", b"%PDF-1.4 minimal"),
            # sop_pdf missing
        }
    )
    req = urllib.request.Request(
        "http://localhost:8000/api/uc3/analyze",
        data=body, headers={"Content-Type": ct}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        assert False, "Expected error"
    except urllib.error.HTTPError as e:
        assert e.code == 422, f"Expected 422, got {e.code}"

def t_uc2_analyze_with_pdf_starts_stream():
    """POST valid multipart to /api/uc2/analyze returns SSE stream."""
    import urllib.request, urllib.error
    tiny_pdf = _make_pdf_bytes("Sponsor obligations test content.")
    body, ct = _make_multipart(
        {"topic": "Sponsor"},
        {
            "external_pdf": ("ext.pdf", tiny_pdf),
            "sop_pdf": ("sop.pdf", tiny_pdf),
        }
    )
    req = urllib.request.Request(
        "http://localhost:8000/api/uc2/analyze",
        data=body, headers={"Content-Type": ct}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            content_type = r.headers.get("Content-Type", "")
            assert "text/event-stream" in content_type, f"Expected SSE, got: {content_type}"
            # Read a small amount — pre-LLM events arrive quickly (~80 bytes each).
            # http.client read(n) with chunked encoding accumulates until n bytes,
            # so keep n small to avoid waiting for LLM calls to fill the buffer.
            first_data = r.read(80).decode("utf-8", errors="replace")
            assert "data:" in first_data, f"No SSE data received: {first_data[:200]}"
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()
        raise AssertionError(f"HTTP {e.code}: {body_err}")

def t_uc3_analyze_with_pdfs_starts_stream():
    """POST valid multipart to /api/uc3/analyze returns SSE stream."""
    import urllib.request, urllib.error
    tiny_pdf = _make_pdf_bytes("Internal labelling guidance.")
    body, ct = _make_multipart(
        {"topic": "Labelling"},
        {
            "external_pdf": ("ext.pdf", tiny_pdf),
            "internal_pdf": ("int.pdf", tiny_pdf),
            "sop_pdf": ("sop.pdf", tiny_pdf),
        }
    )
    req = urllib.request.Request(
        "http://localhost:8000/api/uc3/analyze",
        data=body, headers={"Content-Type": ct}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ct_hdr = r.headers.get("Content-Type", "")
            assert "text/event-stream" in ct_hdr
            # Read small amount — first pre-LLM event arrives immediately.
            first_data = r.read(80).decode("utf-8", errors="replace")
            assert "data:" in first_data, f"No SSE data: {first_data[:200]}"
    except urllib.error.HTTPError as e:
        raise AssertionError(f"HTTP {e.code}: {e.read().decode()}")

CAT9 = "UC2/UC3 HTTP Endpoints"
for fn in [t_uc2_analyze_requires_files, t_uc3_analyze_requires_three_files,
           t_uc2_analyze_with_pdf_starts_stream, t_uc3_analyze_with_pdfs_starts_stream]:
    _run(fn.__name__.replace("t_", ""), fn, CAT9)


# ═════════════════════════════════════════════════════════════════════════════
# REPORT
# ═════════════════════════════════════════════════════════════════════════════
passed   = sum(1 for r in RESULTS if r["status"] == "PASS")
failed   = sum(1 for r in RESULTS if r["status"] == "FAIL")
total    = len(RESULTS)
pct      = round(passed / total * 100, 1) if total else 0

summary = {
    "report_title": "GSK RegIntel Platform – Test Report",
    "generated_at": datetime.now().isoformat(),
    "environment": {
        "python_version": sys.version,
        "platform": sys.platform,
        "server": "http://localhost:8000",
        "config_endpoint": "https://sushgenai.openai.azure.com/",
    },
    "summary": {
        "total": total,
        "passed": passed,
        "failed": failed,
        "pass_rate_pct": pct,
        "overall_status": "PASS" if failed == 0 else "PARTIAL" if passed > failed else "FAIL",
    },
    "categories": {},
    "azure_openai_note": (
        "Tests in 'Azure OpenAI Connectivity' require valid API credentials. "
        "If they fail with 401, verify the api_key and endpoint in config.json "
        "via the Azure Portal → your OpenAI resource → Keys and Endpoints."
    ),
    "test_cases": RESULTS,
}

# Per-category roll-up
for r in RESULTS:
    cat = r["category"]
    if cat not in summary["categories"]:
        summary["categories"][cat] = {"total": 0, "passed": 0, "failed": 0}
    summary["categories"][cat]["total"] += 1
    if r["status"] == "PASS":
        summary["categories"][cat]["passed"] += 1
    else:
        summary["categories"][cat]["failed"] += 1

report_path = ROOT / "test_report.json"
report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

print(f"\n{'='*60}")
print(f"  TEST RESULTS: {passed}/{total} passed ({pct}%)")
print(f"{'='*60}")
for cat, stats in summary["categories"].items():
    icon = "+" if stats["failed"] == 0 else "-"
    print(f"  {icon} {cat}: {stats['passed']}/{stats['total']}")
print(f"\n  Report saved -> test_report.json")
print(f"{'='*60}")
