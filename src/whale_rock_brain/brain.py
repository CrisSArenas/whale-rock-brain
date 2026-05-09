"""Brain Summary generator.

Synthesizes a cross-source narrative from the per-item notes. Two hard rules:

1. Every claim sentence must contain at least one ``[ID]`` reference whose id is
   in the supplied items list.
2. The model is told it operates on a closed world — if it doesn't see evidence
   in the items, it must say so explicitly rather than infer.

Output is validated by a Pydantic model; on validation failure we run a single
repair pass with the specific error before giving up.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from pydantic import ValidationError

from .config import get_ticker_meta
from .llm import call_claude
from .observability import log
from .schemas import BrainSummary, RunMetrics, SourceItem


BRAIN_SYSTEM = """You are the senior alt-data analyst at Whale Rock Capital, a \
TMT-focused long/short hedge fund. You are writing a 5-minute read for a \
portfolio manager who is deciding whether to add to, hold, or trim a \
position. Your audience already knows the sell-side narrative — they want \
what the sell side is missing AND a clear analyst read on what to do with it.

CLOSED WORLD: You may only assert what is in the source items provided. If \
the items don't support a claim, say so explicitly (\"evidence is thin\") \
rather than infer.

PLAIN ENGLISH — non-negotiable. Write like a junior analyst briefing a PM:
  - Short, direct sentences. State the claim, then cite the source.
  - Use numbers when items contain them. Name customers, executives, dollar amounts.
  - Forbidden phrases: \"it remains to be seen\", \"it is worth noting\", \
\"it is important to note\", \"strategically positioned\", \"potentially \
significant\", \"could be a key driver\", \"in light of\", \"while it is true \
that\", \"in the realm of\", \"taken together, the pattern is\". \
Replace any of these with a concrete claim or remove the sentence.

PROMOTIONAL ITEMS (theme tag \"promotional\"): these are pure marketing with \
no commercial substance. Do NOT build the thesis around them. You may cite \
them only to say what is missing — e.g. \"Three press releases ship in six \
months but none name a customer or quote a dollar figure [N-abc][N-def][N-ghi].\"

CITATION RULE — non-negotiable:
Every claim sentence in narrative, whats_changing, bear_case, bull_case, AND \
position_read must reference at least one source item by id in square \
brackets, e.g. \"Operators are migrating off Artifactory [G-7f3a2c].\" Stack \
multiple sources when applicable: \"[H-1a2b][R-c4d5]\". An id not in the \
provided list is hallucination — never invent ids. The watch_list does not \
need citations (it lists future events, not claims about the items).

Output strict JSON only with this exact shape:
{
  "headline": "<= 140 chars. One sentence — the read a PM would forward to their desk.",
  "narrative": "2-3 short paragraphs. Every claim cites [ID]. Plain English, no jargon. End with the cross-source read the sell side is missing.",
  "whats_changing": ["bullet 1 [ID]", "bullet 2 [ID][ID]", ...],   // 3-5 bullets, each cites [ID]. Concrete shifts only — no \"continued momentum\".
  "bear_case": "the non-consensus bear, plain English, with [ID] citations",
  "bull_case": "the non-consensus bull, plain English, with [ID] citations",
  "position_read": "2-3 sentences. What does this data argue FOR or AGAINST? Analyst commentary, NOT a buy/sell rating. Example tone: 'Data leans negative on the AI thesis: zero customers named across three product launches [N-abc][N-def]. Without named customers in the next 8-K, this argues against adding here.' Cite [ID]s.",
  "watch_list": ["Concrete event 1", "Concrete metric 2", ...],   // 2-4 specific future events / metrics that would CONFIRM or INVALIDATE the read. e.g. \"Q4 earnings: Atlas net-new ARR mix\". No [ID] needed.
  "confidence": "Low" | "Medium" | "High",
  "confidence_rationale": "one sentence: why this confidence level",
  "management_questions": ["Question 1 [ID]", "Question 2 [ID][ID]", ...]   // 1-5 SPECIFIC questions to ask management on the next earnings call or 1:1. See Question Calibration below.
}

QUESTION CALIBRATION — match the count to the alt-data depth:
  - 1-2 questions if data is thin (single substantive source, mostly promotional content, or low item count).
  - 2-3 questions if data is moderate (a couple of real themes across 2-3 sources).
  - 4-5 questions if data is rich (multiple substantive themes spanning 3+ sources with concrete commercial detail).

Each question must be:
  - SPECIFIC. Name the metric, the customer, the theme. \"What is the attach rate of Xray to Artifactory in the install base, and how has it trended Q-over-Q?\" beats \"How is the security business?\"
  - ANSWERABLE. Management can give a yes/no/number, not a brand essay.
  - GROUNDED. Cite at least one [ID] from the items that motivates the question. The citation can sit at the end of the question.
  - NON-SOFTBALL. Push on the gap, the contradiction, or the missing proof point — not the pitch deck.

Example good questions:
  \"Three AI-related product launches shipped in six months [N-abc][N-def][N-ghi] but none named a customer or quoted a deal size. Can you walk us through the named-account pipeline for the agentic repository, and what closed-deal contribution it carried in Q3?\"
  \"Operators on r/devops are reporting Artifactory cost-of-ownership escalation tied to the per-artifact pricing change [R-xyz]. What is the gross retention impact you're seeing in the SMB cohort versus enterprise?\"

Example bad questions (do NOT produce these):
  \"Tell us about your AI strategy.\"
  \"How is the competitive environment?\"
  \"What's your outlook for next quarter?\"

Confidence calibration:
  High   — multiple independent sources confirm the same signal with material commercial detail (named customers / $ amounts / metrics).
  Medium — coherent pattern across sources, but key proof points (customers, revenue, named departures) are missing.
  Low    — directionally suggestive but evidence is thin, single-source, or all promotional.
"""


def _format_items_for_brain(items: list[SourceItem]) -> str:
    lines: list[str] = []
    for it in items:
        date = it.published_at.date().isoformat() if it.published_at else "no-date"
        sent = it.sentiment or "neutral"
        rel = it.relevance or 0
        themes = ",".join(it.themes) if it.themes else ""
        summary = it.summary or it.title
        lines.append(
            f"[{it.id}] {it.source} · {date} · sent={sent} · rel={rel} · themes={themes}\n"
            f"    title: {it.title[:200]}\n"
            f"    note: {summary}\n"
        )
    return "\n".join(lines)


def _build_brain_user_prompt(ticker: str, items: list[SourceItem]) -> str:
    meta = get_ticker_meta(ticker)
    company = meta.get("company_name", ticker)
    context = meta.get("context", "")
    return f"""TICKER: {ticker} ({company})
INVESTMENT CONTEXT: {context}

You have {len(items)} alt-data items below. Synthesize the cross-source read \
that an analyst couldn't get from one source alone.

ITEMS:
{_format_items_for_brain(items)}

Now produce the JSON.
"""


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_CITATION_RE = re.compile(r"\[([A-Za-z]-[0-9a-f]{6})\]")


def _extract_json(text: str) -> Optional[dict]:
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _validate_citations(brain: BrainSummary, valid_ids: set[str]) -> list[str]:
    """Return a list of human-readable validation errors. Empty list = OK."""
    errors: list[str] = []

    def find_ids(text: str) -> set[str]:
        return set(_CITATION_RE.findall(text or ""))

    cited: set[str] = set()
    cited |= find_ids(brain.narrative)
    cited |= find_ids(brain.bear_case)
    cited |= find_ids(brain.bull_case)
    cited |= find_ids(brain.position_read)
    for b in brain.whats_changing:
        cited |= find_ids(b)

    # Hallucinated id check.
    bad = cited - valid_ids
    if bad:
        errors.append(
            f"Citations reference ids that are not in the provided items: "
            f"{sorted(bad)}. Remove or replace them with valid ids."
        )

    # Coverage check — every cited section must contain at least one citation.
    if not find_ids(brain.narrative):
        errors.append("narrative contains no [ID] citations; every claim must cite a source.")
    if not find_ids(brain.bear_case):
        errors.append("bear_case contains no [ID] citations.")
    if not find_ids(brain.bull_case):
        errors.append("bull_case contains no [ID] citations.")
    if not find_ids(brain.position_read):
        errors.append("position_read contains no [ID] citations; it must reference items.")
    if not all(find_ids(b) for b in brain.whats_changing):
        errors.append("each whats_changing bullet must cite at least one [ID].")

    # Required field presence.
    if not brain.position_read.strip():
        errors.append("position_read is empty; it must contain analyst commentary.")
    if not brain.watch_list:
        errors.append("watch_list is empty; it must contain 2-4 concrete events to watch.")
    if brain.confidence not in {"Low", "Medium", "High"}:
        errors.append("confidence must be one of Low / Medium / High.")
    if not brain.confidence_rationale.strip():
        errors.append("confidence_rationale is empty.")
    if not brain.management_questions:
        errors.append("management_questions is empty; it must contain 1-5 specific questions for management.")
    elif len(brain.management_questions) > 5:
        errors.append("management_questions must have at most 5 entries.")
    else:
        # At least one question should be grounded in a real cited id.
        any_cited = any(find_ids(q) & valid_ids for q in brain.management_questions)
        if not any_cited:
            errors.append(
                "management_questions must include at least one [ID] citation grounding the questions in the items."
            )

    return errors


async def _call_brain(
    *, system: str, user: str, metrics: RunMetrics, label: str
) -> tuple[Optional[BrainSummary], list[str]]:
    text = await call_claude(
        system=system,
        user=user,
        metrics=metrics,
        label=label,
        max_tokens=2400,
        temperature=0.3,
    )
    parsed = _extract_json(text)
    if not parsed:
        return None, ["model output was not valid JSON"]
    confidence = parsed.get("confidence")
    if confidence not in {"Low", "Medium", "High"}:
        confidence = "Medium"
    try:
        brain = BrainSummary(
            headline=str(parsed.get("headline", ""))[:200],
            narrative=str(parsed.get("narrative", "")),
            whats_changing=[str(x) for x in (parsed.get("whats_changing") or [])][:8],
            bear_case=str(parsed.get("bear_case", "")),
            bull_case=str(parsed.get("bull_case", "")),
            position_read=str(parsed.get("position_read", "")),
            watch_list=[str(x) for x in (parsed.get("watch_list") or [])][:6],
            confidence=confidence,
            confidence_rationale=str(parsed.get("confidence_rationale", ""))[:400],
            management_questions=[str(x) for x in (parsed.get("management_questions") or [])][:5],
            cited_ids=[],
        )
    except ValidationError as ve:
        return None, [str(ve)]
    return brain, []


async def generate_brain(
    ticker: str,
    items: list[SourceItem],
    metrics: RunMetrics,
) -> Optional[BrainSummary]:
    """Run the brain synthesizer, validate, and repair once if needed.

    Filters to relevance >= 3 so the prompt stays signal-heavy. If too few
    items clear the bar, fall back to relevance >= 1 so thin-coverage names
    still produce a Brain Summary (with the Brain explicitly noting the
    evidence is thin).
    """
    if not items:
        return None

    high = [it for it in items if (it.relevance or 0) >= 3]
    if len(high) < 4:
        # Thin coverage — keep everything that wasn't rated as off-topic noise.
        high = [it for it in items if (it.relevance or 0) >= 1]
    # Cap at 30 to keep the brain prompt bounded.
    if len(high) > 30:
        high = sorted(high, key=lambda it: -(it.relevance or 0))[:30]
    if not high:
        return None

    valid_ids = {it.id for it in high}
    user = _build_brain_user_prompt(ticker, high)

    brain, parse_errors = await _call_brain(
        system=BRAIN_SYSTEM, user=user, metrics=metrics, label=f"brain:{ticker}"
    )
    if brain is None:
        log.warning("brain.parse_failed", errors=parse_errors)
        return None

    errors = _validate_citations(brain, valid_ids)
    if errors:
        log.info("brain.repairing", errors=errors)
        repair_user = (
            user
            + "\n\nYour previous output failed validation with these errors:\n- "
            + "\n- ".join(errors)
            + "\n\nProduce a corrected JSON object that fixes every error. "
            "Use ONLY ids from the items list above."
        )
        brain2, parse_errors2 = await _call_brain(
            system=BRAIN_SYSTEM,
            user=repair_user,
            metrics=metrics,
            label=f"brain:{ticker}:repair",
        )
        if brain2 is not None:
            errors2 = _validate_citations(brain2, valid_ids)
            if not errors2:
                brain = brain2
            else:
                log.warning("brain.repair_failed", errors=errors2)
        else:
            log.warning("brain.repair_parse_failed", errors=parse_errors2)

    # Populate cited_ids from the validated text.
    cited: set[str] = set()
    cited |= set(_CITATION_RE.findall(brain.narrative))
    cited |= set(_CITATION_RE.findall(brain.bear_case))
    cited |= set(_CITATION_RE.findall(brain.bull_case))
    cited |= set(_CITATION_RE.findall(brain.position_read))
    for b in brain.whats_changing:
        cited |= set(_CITATION_RE.findall(b))
    for q in brain.management_questions:
        cited |= set(_CITATION_RE.findall(q))
    brain.cited_ids = sorted(cited & valid_ids)
    return brain
