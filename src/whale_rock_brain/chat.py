"""Chat endpoint over a ticker's snapshot.

Closed-world RAG: the model sees the per-item notes (id, source, date, title,
summary, sentiment), the Brain Summary, and the connections list. It is told
to answer ONLY from those items and to cite ids inline. Off-topic or
unsupported questions get an honest "I don't see that in the current data"
response rather than a hallucinated answer.
"""
from __future__ import annotations

import re
import time

from .brain import _format_items_for_brain  # internal reuse
from .config import get_ticker_meta
from .llm import call_claude
from .schemas import ChatMessage, ChatResponse, RunMetrics, TickerSnapshot


CHAT_SYSTEM = """You are an alt-data research assistant embedded in the Whale \
Rock Brain dashboard. You answer a portfolio manager's question about a single \
ticker using ONLY the items currently shown on their dashboard.

Hard rules:
- Closed world. If the answer is not in the items, say so plainly: "The \
current data doesn't show that." Do not infer from outside knowledge.
- Cite item ids inline in square brackets, e.g. "[H-7c1a]". If you cite \
multiple items, stack them: "[R-3f9e][G-1b22]".
- Be direct. No marketing language, no hedge phrases, no recap of the question.
- Keep the answer under 180 words unless the question explicitly asks for a list.
- If the user asks a numeric question, only give a number if you actually see \
it in an item — otherwise say "the items don't quote a figure".

Format: short paragraphs of plain prose. No headings unless asked.
"""


_CITATION_RE = re.compile(r"\[([A-Za-z]-[0-9a-f]{6})\]")


def _format_history(history: list[ChatMessage]) -> str:
    if not history:
        return "(no prior turns)"
    lines = []
    for msg in history[-6:]:  # last 6 turns to keep prompt bounded
        prefix = "PM" if msg.role == "user" else "Assistant"
        lines.append(f"{prefix}: {msg.content[:600]}")
    return "\n".join(lines)


def _format_brain(snapshot: TickerSnapshot) -> str:
    if not snapshot.brain:
        return "(no brain summary available)"
    b = snapshot.brain
    bullets = "\n".join(f"  - {x}" for x in b.whats_changing)
    return (
        f"HEADLINE: {b.headline}\n"
        f"NARRATIVE: {b.narrative}\n"
        f"WHATS_CHANGING:\n{bullets}\n"
        f"BEAR: {b.bear_case}\n"
        f"BULL: {b.bull_case}"
    )


def _format_connections(snapshot: TickerSnapshot) -> str:
    if not snapshot.connections:
        return "(no connections surfaced)"
    lines = []
    for c in snapshot.connections[:10]:
        lines.append(
            f"  - {c.confidence} · {' + '.join(c.item_ids)} · {c.theme} — {c.rationale}"
        )
    return "\n".join(lines)


def _build_user_prompt(snapshot: TickerSnapshot, question: str, history: list[ChatMessage]) -> str:
    meta = get_ticker_meta(snapshot.ticker)
    company = meta.get("company_name", snapshot.ticker)
    age = ""
    if snapshot.refreshed_at:
        age = f"Snapshot refreshed at {snapshot.refreshed_at.isoformat()}.\n"
    return f"""TICKER: {snapshot.ticker} ({company})
{age}You have {len(snapshot.items)} alt-data items, plus the Brain Summary and \
Connections from the dashboard. Answer the PM's question using only this material.

CONVERSATION SO FAR:
{_format_history(history)}

CURRENT QUESTION:
{question}

BRAIN SUMMARY:
{_format_brain(snapshot)}

CONNECTIONS:
{_format_connections(snapshot)}

ITEMS:
{_format_items_for_brain(snapshot.items)}

Answer the PM directly. Cite item ids inline.
"""


async def answer_question(
    snapshot: TickerSnapshot,
    question: str,
    history: list[ChatMessage],
) -> ChatResponse:
    metrics = RunMetrics(label=f"chat:{snapshot.ticker}")
    start = time.perf_counter()
    user = _build_user_prompt(snapshot, question, history)
    answer = await call_claude(
        system=CHAT_SYSTEM,
        user=user,
        metrics=metrics,
        label=f"chat:{snapshot.ticker}",
        max_tokens=900,
        temperature=0.3,
    )
    metrics.duration_seconds = round(time.perf_counter() - start, 3)

    valid_ids = {it.id for it in snapshot.items}
    cited = sorted(set(_CITATION_RE.findall(answer)) & valid_ids)
    return ChatResponse(answer=answer.strip(), cited_ids=cited, metrics=metrics)
