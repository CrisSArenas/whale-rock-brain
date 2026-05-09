"""Anthropic client + per-item summarization.

Single ``AsyncAnthropic`` instance is created lazily on first use and reused.
Each call records its cost into the supplied ``RunMetrics`` so the dashboard
can surface a real per-refresh dollar figure.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional

from anthropic import AsyncAnthropic
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import settings
from .observability import log, record_call
from .schemas import RunMetrics, SourceItem


_CLIENT: Optional[AsyncAnthropic] = None
_SEM = asyncio.Semaphore(4)


def get_client() -> AsyncAnthropic:
    global _CLIENT
    if _CLIENT is None:
        key = settings.anthropic_api_key.get_secret_value()
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env file."
            )
        _CLIENT = AsyncAnthropic(api_key=key)
    return _CLIENT


async def call_claude(
    *,
    system: str,
    user: str,
    metrics: RunMetrics,
    label: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> str:
    """Single-turn Claude call. Returns the raw text from the assistant."""
    client = get_client()
    mt = max_tokens or settings.max_tokens
    temp = settings.temperature if temperature is None else temperature

    async with _SEM:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=20),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                start = time.perf_counter()
                resp = await client.messages.create(
                    model=settings.model,
                    max_tokens=mt,
                    temperature=temp,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                elapsed = time.perf_counter() - start

    text_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    text = "\n".join(text_parts).strip()

    record_call(
        metrics,
        label=label,
        model=settings.model,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        duration_seconds=elapsed,
    )
    return text


# ---------- Per-source-item summarization ----------

SUMMARIZER_SYSTEM = """You are a buy-side analyst at a TMT-focused hedge fund. \
You are reading one piece of alt-data and producing a short note for a \
portfolio manager. Your job is to extract the business-relevant signal in \
plain English — and to flag and demote pure marketing.

Output strict JSON only — no prose around it — with this exact shape:
{
  "summary": "<=240 chars. Plain English. Lead with the substance. \\"Operators say service quality dropped after the Q3 sales reorg\\" beats \\"User on Reddit makes claims about service\\". If there is no business signal, write \\"No business signal — pure marketing\\" or \\"Tangential mention only\\".",
  "sentiment": "bullish" | "bearish" | "neutral" | "mixed",
  "relevance": <integer 1-10>,
  "themes": ["short_tag", ...]   // 1-4 lowercase tags
}

PROMOTIONAL CONTENT — flag and demote.
If the item is a press release, sponsored post, marketing announcement, \
launch hype, or self-promotion WITHOUT commercial substance (no named \
customers, no $ amounts, no usage metrics, no executive specifics), then:
  - Add the theme tag "promotional" to themes.
  - Cap relevance at 2.
  - In the summary, name it: e.g. \"Press release on agentic repository launch — no customers, pricing, or revenue detail.\"

A press release WITH substance (e.g. \"Microsoft signs $5B Azure expansion with Walmart\") is NOT promotional — it carries real signal. Use judgment.

Relevance calibration (be strict — most items are 1-4):
  10  — PM would forward to the desk today. Specific, material, non-consensus.
  7-9 — Strong signal: named customer, pricing detail, executive departure, measurable adoption shift, regulatory event.
  4-6 — Moderate signal: real product launch with partial detail, sector trend that touches this name, substantive operator commentary.
  2-3 — Weak signal: opinion, generic commentary, tangential mention, low-engagement post.
  1   — Off-topic, advertising, pure noise.

Sentiment is the directional read for the SECURITY, not the author's mood:
  - A bullish operator complaint that signals churn is BEARISH for the stock.
  - Promotional content with no substance is NEUTRAL.

PLAIN ENGLISH — no LLM jargon. Forbidden phrases include:
  "it remains to be seen", "it is worth noting", "it is important to note", \
"strategically positioned", "potentially significant", "could be a key driver", \
"in light of", "while it is true that". Just state the claim directly.

Hard rules:
  - Never fabricate numbers, names, dates, or quotes that aren't in the item.
  - Themes should be analyst-actionable (e.g. \"churn_signal\", \"capex_ramp\", \"competitor_intrusion\") — not generic (\"news\", \"tech\").
"""


def _build_item_user_prompt(item: SourceItem, ticker: str, company: str, context: str) -> str:
    return f"""TICKER: {ticker} ({company})
INVESTMENT CONTEXT: {context}

ITEM SOURCE: {item.source}
ITEM ID: {item.id}
ITEM TITLE: {item.title}
ITEM URL: {item.url}

ITEM RAW TEXT:
\"\"\"
{item.raw_text[:2000]}
\"\"\"

Produce the JSON note now.
"""


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    m = _JSON_OBJECT_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def summarize_item(
    item: SourceItem,
    ticker: str,
    company: str,
    context: str,
    metrics: RunMetrics,
) -> SourceItem:
    """Mutates and returns the item with summary/sentiment/relevance/themes filled in.

    On any failure we attach a safe fallback rather than dropping the item, so
    the dashboard always shows something for each fetched source row.
    """
    user = _build_item_user_prompt(item, ticker, company, context)
    try:
        text = await call_claude(
            system=SUMMARIZER_SYSTEM,
            user=user,
            metrics=metrics,
            label=f"summarize:{item.source}:{item.id}",
            max_tokens=400,
            temperature=0.2,
        )
        parsed = _extract_json(text)
    except Exception as exc:
        log.warning("summarize.failed", item_id=item.id, error=str(exc))
        parsed = None

    if not parsed:
        item.summary = (item.title or "")[:240]
        item.sentiment = "neutral"
        item.relevance = 1
        item.themes = []
        return item

    summary = (parsed.get("summary") or "").strip()[:240]
    sentiment = parsed.get("sentiment")
    if sentiment not in {"bullish", "bearish", "neutral", "mixed"}:
        sentiment = "neutral"
    relevance = parsed.get("relevance")
    try:
        relevance = max(1, min(10, int(relevance)))
    except (TypeError, ValueError):
        relevance = 5
    themes = parsed.get("themes") or []
    if not isinstance(themes, list):
        themes = []
    themes = [str(t).strip().lower() for t in themes if str(t).strip()][:4]

    item.summary = summary or (item.title or "")[:240]
    item.sentiment = sentiment
    item.relevance = relevance
    item.themes = themes
    return item


async def summarize_items(
    items: list[SourceItem],
    ticker: str,
    company: str,
    context: str,
    metrics: RunMetrics,
) -> list[SourceItem]:
    coros = [summarize_item(it, ticker, company, context, metrics) for it in items]
    return await asyncio.gather(*coros)
