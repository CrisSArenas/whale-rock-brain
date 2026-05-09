"""Pydantic models that flow across module boundaries."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


SourceName = Literal["reddit", "hackernews", "x", "news", "github", "edgar"]
Sentiment = Literal["bullish", "bearish", "neutral", "mixed"]
Confidence = Literal["Low", "Medium", "High"]
TimeWindow = Literal["1week", "1month", "3months", "6months", "1year"]


# Translate a logical window into days (used by HN, GitHub, news).
WINDOW_DAYS: dict[TimeWindow, int] = {
    "1week": 7,
    "1month": 30,
    "3months": 90,
    "6months": 180,
    "1year": 365,
}

# Reddit's `t=` only supports a fixed list of bucket names.
WINDOW_REDDIT: dict[TimeWindow, str] = {
    "1week": "week",
    "1month": "month",
    "3months": "year",
    "6months": "year",
    "1year": "year",
}

# Google News uses a `when:` operator inside the query string.
WINDOW_NEWS: dict[TimeWindow, str] = {
    "1week": "7d",
    "1month": "1m",
    "3months": "3m",
    "6months": "6m",
    "1year": "1y",
}


class SourceItem(BaseModel):
    """Normalized item across all sources."""

    id: str  # short, stable, citable id like R-7f3a, H-12345, G-jfrog/artifactory#9281
    source: SourceName
    title: str
    url: str
    author: Optional[str] = None
    published_at: Optional[datetime] = None
    raw_text: str = ""  # truncated body / comments / description
    metadata: dict = Field(default_factory=dict)

    # Filled in by the LLM summarizer pass
    summary: Optional[str] = None
    sentiment: Optional[Sentiment] = None
    relevance: Optional[int] = None  # 1-10
    themes: list[str] = Field(default_factory=list)


class Connection(BaseModel):
    """A judged link between two source items."""

    item_ids: list[str]  # 2 ids
    theme: str
    rationale: str  # what the LLM saw
    confidence: Confidence
    similarity: float  # raw cosine score from TF-IDF


class CostRecord(BaseModel):
    """Per-LLM-call cost trace."""

    label: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_seconds: float
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class RunMetrics(BaseModel):
    """Aggregated metrics for one refresh / one chat turn."""

    label: str  # e.g. "refresh:AMD" or "chat:AMD"
    started_at: datetime = Field(default_factory=datetime.utcnow)
    duration_seconds: float = 0.0
    calls: list[CostRecord] = Field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 4)

    @property
    def total_input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def total_output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)


class BrainSummary(BaseModel):
    """The headline output. Every claim must cite at least one item id."""

    headline: str  # one-line read of the cross-source narrative
    narrative: str  # 2-4 paragraphs synthesizing across sources, every claim cites [ID]
    whats_changing: list[str]  # bullet list of recent shifts, each cites [ID]
    bear_case: str  # the non-consensus bear, cites [ID]
    bull_case: str  # the non-consensus bull, cites [ID]

    # PM-actionable fields (added v0.2)
    position_read: str  # "What this argues for / against." Analyst commentary, not a buy/sell rating.
    watch_list: list[str]  # 2-4 concrete events / metrics that would confirm or invalidate the read
    confidence: Confidence  # Low / Medium / High
    confidence_rationale: str  # one sentence: why this confidence level

    # Next action items: 1-5 questions to ask management. Count scales with the
    # depth/quality of the alt-data: thin coverage gets fewer, rich coverage gets more.
    management_questions: list[str] = Field(default_factory=list)

    cited_ids: list[str]  # all ids referenced anywhere


class TickerSnapshot(BaseModel):
    """Everything stored for one ticker — read by the dashboard."""

    ticker: str
    company_name: str
    refreshed_at: datetime
    time_window: TimeWindow = "1month"
    items: list[SourceItem]
    brain: Optional[BrainSummary] = None
    connections: list[Connection] = Field(default_factory=list)
    last_run_metrics: Optional[RunMetrics] = None
    source_status: dict[str, str] = Field(default_factory=dict)
    # Per-source freshness/health tag, e.g. {"reddit": "ok: 23 items", "github": "rate-limited"}


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    cited_ids: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    ticker: str
    question: str
    history: list[ChatMessage] = Field(default_factory=list)


class ChatResponse(BaseModel):
    answer: str
    cited_ids: list[str]
    metrics: RunMetrics
