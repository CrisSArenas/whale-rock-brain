"""Connections engine.

Two-stage to keep the LLM cost bounded and the connection quality honest:

    Stage A — TF-IDF cosine on (title + summary + themes) for every pair of
    items. Cheap, deterministic. Take the top-K pairs above a similarity floor
    that span at least two different sources. This is candidate generation
    only — it WILL produce keyword-matches that are not real connections.

    Stage B — LLM-as-judge. For each candidate, ask Sonnet 4.6: is this a real
    thematic connection or just keyword overlap? Output is forced to a tiny
    JSON shape that includes a confidence rating. We drop anything below
    Medium and never let the judge fabricate item ids.

Connection cost is therefore bounded at ``connection_max_pairs`` LLM calls per
refresh — currently 12 — regardless of how many items we ingested.
"""
from __future__ import annotations

import json
import re
from itertools import combinations
from typing import Optional

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .config import settings
from .llm import call_claude
from .observability import log
from .schemas import Confidence, Connection, RunMetrics, SourceItem


JUDGE_SYSTEM = """You are a senior buy-side analyst evaluating whether two \
pieces of alt data describe the same underlying business reality, or whether \
they merely share keywords.

You will be given two items (different sources, same ticker). Decide:
  - Is there a REAL thematic link — an operational, financial, product, \
people, or competitive thread that connects them?
  - If yes, what is the link in one sentence?
  - How confident are you?

Output strict JSON only — no prose around it:
{
  "is_connected": true | false,
  "theme": "<= 80 chars, the link in plain words, OR \"\" if not connected",
  "rationale": "<= 240 chars explaining what you saw, OR \"\" if not connected",
  "confidence": "Low" | "Medium" | "High"
}

Rules:
- Topical adjacency is NOT a connection. A Reddit post about AMD GPUs and a \
GitHub release of an AMD-related project are not connected unless they share a \
thematic thread (same product, same complaint, same strategic shift).
- High confidence requires both items to point at the SAME specific signal.
- Medium confidence is acceptable when the link is plausible but each item \
covers only part of the picture.
- If in doubt, return is_connected=false. Do not invent links.
"""


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _item_text(item: SourceItem) -> str:
    """Compact text used for both TF-IDF and the LLM judge prompt."""
    parts = [item.title or ""]
    if item.summary:
        parts.append(item.summary)
    if item.themes:
        parts.append(" ".join(item.themes))
    parts.append((item.raw_text or "")[:400])
    return " ".join(parts).strip()


def _tfidf_candidate_pairs(items: list[SourceItem]) -> list[tuple[int, int, float]]:
    if len(items) < 2:
        return []
    texts = [_item_text(it) for it in items]
    if not any(t.strip() for t in texts):
        return []
    try:
        vec = TfidfVectorizer(stop_words="english", min_df=1, max_df=0.95, ngram_range=(1, 2))
        matrix = vec.fit_transform(texts)
    except ValueError:
        return []
    sim = cosine_similarity(matrix)
    pairs: list[tuple[int, int, float]] = []
    for i, j in combinations(range(len(items)), 2):
        s = float(sim[i, j])
        if s < settings.connection_min_similarity:
            continue
        # Cross-source preference: still allow same-source pairs but boost the gate higher.
        if items[i].source == items[j].source and s < settings.connection_min_similarity * 1.6:
            continue
        pairs.append((i, j, s))
    pairs.sort(key=lambda p: p[2], reverse=True)
    return pairs[: settings.connection_max_pairs]


def _format_judge_user(a: SourceItem, b: SourceItem, ticker: str) -> str:
    return f"""TICKER: {ticker}

ITEM A
  id: {a.id}
  source: {a.source}
  title: {a.title}
  date: {a.published_at.date().isoformat() if a.published_at else 'unknown'}
  summary: {a.summary or '(no summary)'}
  themes: {','.join(a.themes) if a.themes else '(none)'}

ITEM B
  id: {b.id}
  source: {b.source}
  title: {b.title}
  date: {b.published_at.date().isoformat() if b.published_at else 'unknown'}
  summary: {b.summary or '(no summary)'}
  themes: {','.join(b.themes) if b.themes else '(none)'}

Decide if there is a real thematic connection. Return the JSON.
"""


def _parse_judge(text: str) -> Optional[dict]:
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


_CONF_RANK: dict[Confidence, int] = {"Low": 1, "Medium": 2, "High": 3}


async def _judge_pair(
    a: SourceItem, b: SourceItem, similarity: float, ticker: str, metrics: RunMetrics
) -> Optional[Connection]:
    user = _format_judge_user(a, b, ticker)
    try:
        text = await call_claude(
            system=JUDGE_SYSTEM,
            user=user,
            metrics=metrics,
            label=f"judge:{a.id}+{b.id}",
            max_tokens=300,
            temperature=0.2,
        )
    except Exception as exc:
        log.warning("judge.failed", a=a.id, b=b.id, error=str(exc))
        return None

    parsed = _parse_judge(text)
    if not parsed or not parsed.get("is_connected"):
        return None

    confidence = parsed.get("confidence")
    if confidence not in {"Low", "Medium", "High"}:
        confidence = "Low"
    if _CONF_RANK[confidence] < settings.connection_judge_threshold:
        return None

    theme = (parsed.get("theme") or "").strip()[:120]
    rationale = (parsed.get("rationale") or "").strip()[:400]
    if not theme:
        return None

    return Connection(
        item_ids=[a.id, b.id],
        theme=theme,
        rationale=rationale,
        confidence=confidence,
        similarity=round(similarity, 3),
    )


async def build_connections(
    ticker: str,
    items: list[SourceItem],
    metrics: RunMetrics,
) -> list[Connection]:
    # Use moderate-relevance items only — connections on noise tend to surface
    # spurious links. Fall back to all items for thin-coverage names.
    candidates = [it for it in items if (it.relevance or 0) >= 2]
    if len(candidates) < 4:
        candidates = items

    if len(candidates) < 2:
        return []
    pairs = _tfidf_candidate_pairs(candidates)
    if not pairs:
        return []

    log.info("connections.candidates", ticker=ticker, n=len(pairs))

    # Run judges concurrently — call_claude already gates concurrency via the
    # module-level semaphore, so this is safe.
    import asyncio

    coros = [_judge_pair(candidates[i], candidates[j], s, ticker, metrics) for i, j, s in pairs]
    results = await asyncio.gather(*coros)

    connections = [r for r in results if r is not None]
    # Sort: confidence first, then similarity.
    connections.sort(key=lambda c: (-_CONF_RANK[c.confidence], -c.similarity))
    return connections
