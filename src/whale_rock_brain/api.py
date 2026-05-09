"""FastAPI application surface.

Endpoints
---------
GET  /                      → serves the dashboard HTML
GET  /api/tickers           → list of supported tickers + last refresh times
GET  /api/dashboard/{tk}    → cached snapshot (404 if not refreshed yet)
POST /api/refresh/{tk}      → run the full pipeline, save, return new snapshot
POST /api/chat              → answer a question against the current snapshot
GET  /health                → liveness probe
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .chat import answer_question
from .config import PROJECT_ROOT, load_tickers, settings
from .orchestrator import refresh_ticker
from .schemas import ChatRequest, ChatResponse, TickerSnapshot, TimeWindow
from .storage import load_snapshot, snapshot_age_seconds


FRONTEND_DIR = PROJECT_ROOT / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"


app = FastAPI(
    title="Whale Rock Brain",
    description="Alt-data dashboard for TMT names. Five sources, one synthesized read.",
    version="0.1.0",
)


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    if not INDEX_HTML.exists():
        raise HTTPException(status_code=500, detail="frontend/index.html not found")
    return FileResponse(INDEX_HTML)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "model": settings.model,
        "anthropic_key_set": bool(settings.anthropic_api_key.get_secret_value()),
    }


@app.get("/api/tickers")
async def list_tickers() -> dict:
    tickers = load_tickers()
    rows = []
    for symbol, meta in tickers.items():
        age = snapshot_age_seconds(symbol)
        rows.append({
            "ticker": symbol,
            "company_name": meta.get("company_name", symbol),
            "context": meta.get("context", ""),
            "group": meta.get("group", "case_study"),
            "snapshot_age_seconds": age,
            "has_snapshot": age is not None,
        })
    return {"tickers": rows}


@app.get("/api/dashboard/{ticker}")
async def get_dashboard(ticker: str) -> TickerSnapshot:
    snap = load_snapshot(ticker.upper())
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No snapshot for {ticker.upper()} yet. "
                "POST /api/refresh/{ticker} to build one."
            ),
        )
    return snap


@app.post("/api/refresh/{ticker}")
async def refresh(
    ticker: str,
    window: TimeWindow = Query("1month", description="Time window for source ingestion."),
) -> TickerSnapshot:
    tickers = load_tickers()
    if ticker.upper() not in tickers:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown ticker {ticker!r}. Known: {list(tickers)}",
        )
    if not settings.anthropic_api_key.get_secret_value():
        raise HTTPException(
            status_code=400,
            detail="ANTHROPIC_API_KEY is not set. Add it to .env and restart.",
        )
    return await refresh_ticker(ticker, time_window=window)


@app.post("/api/chat")
async def chat(req: ChatRequest) -> ChatResponse:
    snap = load_snapshot(req.ticker.upper())
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail=f"No snapshot for {req.ticker.upper()}. Refresh it first.",
        )
    if not settings.anthropic_api_key.get_secret_value():
        raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY is not set.")
    return await answer_question(snap, req.question, req.history)


# Optionally serve a /static directory if you ever drop assets there.
_STATIC_DIR = FRONTEND_DIR / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
