"""Refresh orchestrator. One entry point: ``refresh_ticker``.

Pipeline:
    1. Ingest from all 6 sources concurrently. Each source returns (items, status).
       Sources except EDGAR honor the requested ``time_window`` (1 week …
       1 year). EDGAR always returns the most recent filings regardless of
       window — the analyst always wants the latest authoritative anchor.
    2. Cross-refresh deduplication: items whose ids appear in the previous
       snapshot are reused with their existing summary. Only new items go to
       the per-item LLM summarizer.
    3. Brain Summary (one LLM call + optional repair).
    4. Connections (TF-IDF candidates + LLM judge per pair).
    5. Save the snapshot to disk.

The full set of summarized items is preserved on the snapshot regardless of
relevance score. The Brain Summary and Connection engine internally focus on
high-relevance items so the synthesis stays signal-heavy, but the dashboard
shows everything that came back so the analyst can scan thin-coverage names
without losing context.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from .brain import generate_brain
from .config import get_ticker_meta
from .connections import build_connections
from .llm import summarize_items
from .observability import log
from .schemas import RunMetrics, SourceItem, TickerSnapshot, TimeWindow
from .sources import (
    edgar_source,
    github_source,
    hn_source,
    news_source,
    reddit_source,
    x_source,
)
from .storage import load_snapshot, save_snapshot


SOURCE_LIMITS = {
    "reddit": 22,
    "hackernews": 16,
    "x": 12,
    "news": 10,
    "github": 12,
    "edgar": 8,
}


async def _safe_fetch(name: str, coro) -> tuple[str, list[SourceItem], str]:
    try:
        result = await coro
        if isinstance(result, tuple):
            items, status = result
        else:
            items, status = result, "ok"
        return name, items, status
    except Exception as exc:
        log.warning("source.fetch_exception", source=name, error=str(exc))
        return name, [], f"failed: {exc}"


def _trim_per_source(items: list[SourceItem]) -> list[SourceItem]:
    """Cap items per source so a single noisy source can't dominate the LLM budget."""
    out: list[SourceItem] = []
    by_source: dict[str, int] = {}
    items_sorted = sorted(
        items,
        key=lambda it: (
            -len(it.raw_text or ""),
            -(it.published_at.timestamp() if it.published_at else 0),
        ),
    )
    for it in items_sorted:
        cap = SOURCE_LIMITS.get(it.source, 12)
        if by_source.get(it.source, 0) >= cap:
            continue
        out.append(it)
        by_source[it.source] = by_source.get(it.source, 0) + 1
    return out


def _merge_with_previous(
    new_items: list[SourceItem], previous: TickerSnapshot | None
) -> tuple[list[SourceItem], list[SourceItem], int]:
    if previous is None or not previous.items:
        return [], new_items, 0
    prev_by_id: dict[str, SourceItem] = {it.id: it for it in previous.items}
    already: list[SourceItem] = []
    todo: list[SourceItem] = []
    for it in new_items:
        prior = prev_by_id.get(it.id)
        if prior is not None and prior.summary:
            it.summary = prior.summary
            it.sentiment = prior.sentiment
            it.relevance = prior.relevance
            it.themes = prior.themes
            already.append(it)
        else:
            todo.append(it)
    return already, todo, len(already)


async def refresh_ticker(
    ticker: str, time_window: TimeWindow = "1month"
) -> TickerSnapshot:
    ticker = ticker.upper()
    meta = get_ticker_meta(ticker)
    company = meta.get("company_name", ticker)
    context = meta.get("context", "")

    metrics = RunMetrics(label=f"refresh:{ticker}")
    start = time.perf_counter()
    log.info("refresh.start", ticker=ticker, time_window=time_window)

    # Stage 1 — concurrent ingestion across all six sources.
    fetches = await asyncio.gather(
        _safe_fetch("reddit", reddit_source.fetch(meta, time_window)),
        _safe_fetch("hackernews", hn_source.fetch(meta, time_window)),
        _safe_fetch("x", x_source.fetch(ticker, meta, time_window)),
        _safe_fetch("news", news_source.fetch(ticker, meta, time_window)),
        _safe_fetch("github", github_source.fetch(meta, time_window)),
        _safe_fetch("edgar", edgar_source.fetch(meta, ticker)),
    )

    all_items: list[SourceItem] = []
    source_status: dict[str, str] = {}
    for name, items, status in fetches:
        source_status[name] = status
        all_items.extend(items)

    log.info(
        "refresh.ingested",
        ticker=ticker,
        time_window=time_window,
        total=len(all_items),
        per_source={n: len(i) for n, i, _ in fetches},
    )

    capped = _trim_per_source(all_items)

    # Stage 2 — cross-refresh dedup, then summarize only the new items.
    previous = load_snapshot(ticker)
    already, todo, reused = _merge_with_previous(capped, previous)
    if reused:
        log.info("refresh.dedup", ticker=ticker, reused=reused, new=len(todo))

    if todo:
        todo = await summarize_items(todo, ticker, company, context, metrics)

    summarized = already + todo

    # Drop pure-promotional noise — items the model flagged as marketing/press
    # release with no commercial substance (relevance <= 2 AND tagged
    # "promotional"). These add no PM-relevant signal and clutter the feeds.
    def _is_pure_promo(it: SourceItem) -> bool:
        themes = [t.lower() for t in (it.themes or [])]
        return "promotional" in themes and (it.relevance or 0) <= 2

    pre = len(summarized)
    summarized = [it for it in summarized if not _is_pure_promo(it)]
    dropped_promo = pre - len(summarized)
    if dropped_promo:
        log.info("refresh.promo_filtered", ticker=ticker, dropped=dropped_promo)

    # Sort items by source then relevance desc — the UI displays them in this order.
    summarized.sort(
        key=lambda it: (it.source, -(it.relevance or 0), -(it.published_at.timestamp() if it.published_at else 0))
    )

    # Stage 3 — Brain Summary (uses high-relevance subset internally).
    brain = await generate_brain(ticker, summarized, metrics) if summarized else None

    # Stage 4 — Connections (uses moderate-relevance subset internally).
    connections = await build_connections(ticker, summarized, metrics) if summarized else []

    metrics.duration_seconds = round(time.perf_counter() - start, 3)

    snap = TickerSnapshot(
        ticker=ticker,
        company_name=company,
        refreshed_at=datetime.utcnow(),
        time_window=time_window,
        items=summarized,
        brain=brain,
        connections=connections,
        last_run_metrics=metrics,
        source_status=source_status,
    )
    save_snapshot(snap)
    log.info(
        "refresh.complete",
        ticker=ticker,
        items=len(summarized),
        connections=len(connections),
        cost_usd=metrics.total_cost_usd,
        duration_s=metrics.duration_seconds,
        reused_summaries=reused,
    )
    return snap
