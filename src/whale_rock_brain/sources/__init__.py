"""Source ingestors. Each module exposes ``async fetch(...)`` returning (items, status)."""
from . import (
    edgar_source,
    github_source,
    hn_source,
    news_source,
    reddit_source,
    x_source,
)

__all__ = [
    "reddit_source",
    "hn_source",
    "x_source",
    "news_source",
    "github_source",
    "edgar_source",
]
