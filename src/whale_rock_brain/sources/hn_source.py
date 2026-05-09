"""Hacker News ingestion via the Algolia public search API.

The Algolia HN endpoint (``hn.algolia.com/api/v1/search_by_date``) is documented,
free, requires no key, and returns full-text matches across stories, comments,
and Ask/Show submissions. Especially valuable for TMT names — HN often surfaces
developer/operator narrative weeks before sell-side picks it up (job posting
roundups, founder commentary, infra post-mortems).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import httpx

import time as _time

from ..config import settings
from ..observability import log
from ..schemas import SourceItem, TimeWindow, WINDOW_DAYS


HN_BASE = "https://hn.algolia.com/api/v1/search_by_date"
TIMEOUT = 10.0


def _short_id(raw: str) -> str:
    return f"H-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:6]}"


def _hit_to_item(hit: dict[str, Any]) -> SourceItem | None:
    obj_id = hit.get("objectID")
    if not obj_id:
        return None
    title = (hit.get("title") or hit.get("story_title") or "").strip()
    text = (hit.get("comment_text") or hit.get("story_text") or "").strip()
    url = hit.get("url") or hit.get("story_url") or f"https://news.ycombinator.com/item?id={obj_id}"
    author = hit.get("author") or "unknown"
    points = hit.get("points")
    num_comments = hit.get("num_comments")
    created_at = hit.get("created_at_i")
    published = (
        datetime.fromtimestamp(created_at, tz=timezone.utc).replace(tzinfo=None)
        if isinstance(created_at, (int, float))
        else None
    )

    is_comment = bool(hit.get("comment_text"))
    kind = "comment" if is_comment else "story"
    if not title and is_comment:
        # Fall back to first line of the comment for display.
        first_line = text.split("\n", 1)[0][:140]
        title = first_line or "HN comment"

    parts = [f"[HN {kind}", f"by {author}"]
    if points is not None:
        parts.append(f"{points} pts")
    if num_comments is not None:
        parts.append(f"{num_comments} comments")
    parts.append("]")
    header = " · ".join(parts).replace(" · ]", "]")

    body = text[:1500]
    raw_text = f"{header}\n{title}\n\n{body}".strip()

    return SourceItem(
        id=_short_id(obj_id),
        source="hackernews",
        title=title or f"HN {kind} {obj_id}",
        url=url,
        author=author,
        published_at=published,
        raw_text=raw_text,
        metadata={
            "hn_id": obj_id,
            "kind": kind,
            "points": points,
            "num_comments": num_comments,
        },
    )


async def fetch(
    ticker_meta: dict[str, Any], time_window: TimeWindow = "1month"
) -> tuple[list[SourceItem], str]:
    aliases: list[str] = ticker_meta.get("aliases") or []
    company_name: str = ticker_meta.get("company_name") or ""
    if not aliases and not company_name:
        return [], "skipped: no aliases or company name"

    # HN's Algolia API does NOT support boolean OR in `query` — it treats OR
    # as a literal word and AND-matches everything. Instead we use the most
    # distinctive single term as `query` and pass the rest via optionalWords
    # which boosts but doesn't require those words.
    primary = company_name or aliases[0]
    optional = [a for a in aliases if a.lower() != primary.lower()]
    days = WINDOW_DAYS.get(time_window, 30)
    cutoff = int(_time.time()) - days * 86400
    params = {
        "query": primary,
        "tags": "(story,comment)",
        "hitsPerPage": str(settings.hn_items),
        "numericFilters": f"created_at_i>{cutoff}",
    }
    if optional:
        params["optionalWords"] = " ".join(optional[:4])
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(HN_BASE, params=params)
            if r.status_code != 200:
                log.warning("hn.http_error", status=r.status_code)
                return [], f"failed: HTTP {r.status_code}"
            data = r.json()
    except Exception as exc:
        log.warning("hn.fetch_failed", error=str(exc))
        return [], f"failed: {exc}"

    items: list[SourceItem] = []
    for hit in data.get("hits", []):
        # Pre-filter: drop hits with no engagement at all. A 0-point HN story
        # or comment is rarely worth a Sonnet 4.6 call.
        points = hit.get("points") or 0
        num_comments = hit.get("num_comments") or 0
        if points < 1 and num_comments < 1 and not (hit.get("comment_text") or hit.get("story_text")):
            continue
        item = _hit_to_item(hit)
        if item is not None and (item.title or item.raw_text):
            items.append(item)

    if not items:
        return [], "ok: 0 items (no recent HN matches)"
    return items, f"ok: {len(items)} items"
