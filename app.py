"""
GSK Regulatory Intelligence – Unified Web Application
FastAPI backend serving a single-page frontend.

Run with:
    uvicorn app:app --reload --port 8000
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="GSK RegIntel Platform", version="1.0.0")

# Allow local dev requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static assets (CSS, images, etc.)
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Serve frontend ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = static_dir / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "app": "GSK RegIntel Platform"}


# ── UC1 – Regulatory Intelligence Chatbot ─────────────────────────────────────

class ChatMessage(BaseModel):
    role: str   # "user" | "assistant"
    content: str

class ChatRequest(BaseModel):
    history: list[ChatMessage]
    message: str


@app.post("/api/uc1/chat")
async def uc1_chat(request: ChatRequest):
    """
    Streaming chat endpoint for the RegIntel Chatbot (UC1).
    Returns Server-Sent Events.
    """
    from langchain_core.messages import AIMessage, HumanMessage
    from agents.uc1_chatbot import load_uc1_assets, search_and_reconstruct, stream_rag_response, transform_query

    # Build LangChain history
    lc_history = []
    for msg in request.history:
        if msg.role == "user":
            lc_history.append(HumanMessage(content=msg.content))
        else:
            lc_history.append(AIMessage(content=msg.content))
    lc_history.append(HumanMessage(content=request.message))

    import asyncio
    # Load assets in a thread to avoid blocking the event loop (FAISS load ~4s)
    assets = await asyncio.to_thread(load_uc1_assets)
    loop = asyncio.get_running_loop()

    async def _generate():
        # Yield immediately so SSE stream is established before any blocking I/O
        yield f"data: {json.dumps({'type': 'status', 'content': 'Processing query...'})}\n\n"
        try:
            # 1. Query transformation (blocking — run in thread)
            qte = await loop.run_in_executor(
                None, lambda: transform_query(lc_history, assets["qte_system"], assets["glossary"])
            )
            yield f"data: {json.dumps({'type': 'qte', 'content': qte})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
            return

        # 2. Knowledge base search (blocking — run in thread)
        try:
            knowledge = await loop.run_in_executor(None, lambda: search_and_reconstruct(qte))
        except Exception as exc:
            knowledge = []
            yield f"data: {json.dumps({'type': 'kb_error', 'content': str(exc)})}\n\n"

        kb_rows = [{"Title": k["Title"], "Score": float(round(k["Score"] * 100, 1)), "Page": k["PageNumber"]} for k in knowledge]
        yield f"data: {json.dumps({'type': 'kb', 'content': kb_rows})}\n\n"

        # 3. Stream RAG response (blocking iterator — collect in thread then stream tokens)
        full_response = ""
        try:
            def _collect_rag():
                return list(stream_rag_response(
                    lc_history, qte, knowledge,
                    assets["rag_system"], assets["rag_user"], assets["glossary"]
                ))
            tokens = await loop.run_in_executor(None, _collect_rag)
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
            return

        for chunk in tokens:
            full_response += chunk
            yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'content': full_response})}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")


# ── UC2 – External RegIntel Risk Analyzer ────────────────────────────────────

@app.post("/api/uc2/analyze")
async def uc2_analyze(
    topic: Annotated[str, Form()],
    external_pdf: Annotated[UploadFile, File()],
    sop_pdf: Annotated[UploadFile, File()],
):
    """Streaming SSE pipeline for UC2."""
    from agents.uc2_risk_analyzer import run_pipeline

    ext_bytes = await external_pdf.read()
    sop_bytes = await sop_pdf.read()

    def _generate():
        for event in run_pipeline(topic, ext_bytes, sop_bytes):
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: {\"stage\": \"stream_end\"}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")


# ── UC3 – Dual Source Analyzer ────────────────────────────────────────────────

@app.post("/api/uc3/analyze")
async def uc3_analyze(
    topic: Annotated[str, Form()],
    external_pdf: Annotated[UploadFile, File()],
    internal_pdf: Annotated[UploadFile, File()],
    sop_pdf: Annotated[UploadFile, File()],
):
    """Streaming SSE pipeline for UC3."""
    from agents.uc3_dual_analyzer import run_pipeline

    ext_bytes = await external_pdf.read()
    int_bytes = await internal_pdf.read()
    sop_bytes = await sop_pdf.read()

    def _generate():
        for event in run_pipeline(topic, ext_bytes, int_bytes, sop_bytes):
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: {\"stage\": \"stream_end\"}\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream")


# ── Topics endpoints ──────────────────────────────────────────────────────────

@app.get("/api/uc2/topics")
async def uc2_topics():
    from agents.uc2_risk_analyzer import TOPICS
    return {"topics": TOPICS}

@app.get("/api/uc3/topics")
async def uc3_topics():
    from agents.uc3_dual_analyzer import TOPICS
    return {"topics": TOPICS}
