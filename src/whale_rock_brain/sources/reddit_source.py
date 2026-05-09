"""Reddit ingestion via the public ``.json`` endpoints.

Reddit exposes ``https://www.reddit.com/r/{sub}/search.json`` as a public,
documented JSON endpoint. We hit it with a browser-class User-Agent (Reddit
fingerprints generic clients) and gentle inter-subreddit pacing. No auth,
no API key, no setup required.

For each ticker, ``data/tickers.json`` declares the relevant subreddits.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import settings
from ..observability import log
from ..schemas import SourceItem, TimeWindow, WINDOW_REDDIT


REDDIT_BASE = "https://www.reddit.com"
TIMEOUT = 12.0

# Browser-class UA — Reddit blocks generic clients but allows real-looking ones.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _short_id(prefix: str, raw: str) -> str:
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
    return f"{prefix}-{h}"


async def _fetch_subreddit(
    client: httpx.AsyncClient,
    subreddit: str,
    query: str,
    limit: int,
    t_bucket: str,
) -> list[dict[str, Any]] | None:
    """Return the list of post objects for one subreddit, or None on rate-limit."""
    url = f"{REDDIT_BASE}/r/{subreddit}/search.json"
    params = {
        "q": query,
        "restrict_sr": "1",
        "sort": "new",
        "t": t_bucket,
        "limit": str(limit),
    }
    try:
        r = await client.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 429:
            return None
        if r.status_code != 200:
            log.warning("reddit.http_error", subreddit=subreddit, status=r.status_code)
            return []
        data = r.json()
        return [c.get("data", {}) for c in data.get("data", {}).get("children", [])]
    except Exception as exc:
        log.warning("reddit.fetch_failed", subreddit=subreddit, error=str(exc))
        return []


def _post_to_item(post: dict[str, Any], subreddit: str) -> SourceItem:
    pid = post.get("id", "")
    title = (post.get("title") or "").strip()
    selftext = (post.get("selftext") or "").strip()
    permalink = post.get("permalink") or ""
    url = f"{REDDIT_BASE}{permalink}" if permalink else (post.get("url") or "")
    author = post.get("author") or "unknown"
    created_utc = post.get("created_utc")
    published = (
        datetime.fromtimestamp(created_utc, tz=timezone.utc).replace(tzinfo=None)
        if isinstance(created_utc, (int, float))
        else None
    )
    score = post.get("score") or 0
    num_comments = post.get("num_comments") or 0

    body = selftext[:1500]
    raw_text = f"[r/{subreddit} · score {score} · {num_comments} comments]\n{title}\n\n{body}"

    return SourceItem(
        id=_short_id("R", f"{subreddit}-{pid}"),
        source="reddit",
        title=title or f"r/{subreddit} post",
        url=url,
        author=author,
        published_at=published,
        raw_text=raw_text.strip(),
        metadata={
            "subreddit": subreddit,
            "score": score,
            "num_comments": num_comments,
            "reddit_id": pid,
        },
    )


async def fetch(
    ticker_meta: dict[str, Any], time_window: TimeWindow = "1month"
) -> tuple[list[SourceItem], str]:
    subreddits: list[str] = ticker_meta.get("subreddits") or []
    aliases: list[str] = ticker_meta.get("aliases") or []
    if not subreddits or not aliases:
        return [], "skipped: no subreddits or aliases configured"

    query = " OR ".join(f'"{a}"' for a in aliases[:4])
    limit = settings.reddit_items_per_sub
    t_bucket = WINDOW_REDDIT.get(time_window, "month")

    async with httpx.AsyncClient(headers=HEADERS) as client:
        all_items: list[SourceItem] = []
        failures = 0
        rate_limited = False
        for sub in subreddits:
            posts = await _fetch_subreddit(client, sub, query, limit, t_bucket)
            if posts is None:
                rate_limited = True
                continue
            if not posts:
                failures += 1
                continue
            for post in posts:
                if not post.get("title"):
                    continue
                # Pre-filter: drop pure noise. Keep posts that either have
                # comments, score above 1, or meaningful self-text. This
                # filters obvious junk before paying for an LLM summary.
                score = post.get("score") or 0
                num_comments = post.get("num_comments") or 0
                selftext_len = len((post.get("selftext") or "").strip())
                if score < 2 and num_comments < 1 and selftext_len < 80:
                    continue
                all_items.append(_post_to_item(post, sub))
            await asyncio.sleep(0.6)  # gentle pacing

    seen: set[str] = set()
    unique: list[SourceItem] = []
    for item in all_items:
        if item.id in seen:
            continue
        seen.add(item.id)
        unique.append(item)

    if not unique:
        if rate_limited:
            return [], "rate-limited (Reddit asked us to slow down)"
        return [], f"failed: 0 items across {failures}/{len(subreddits)} subreddits"
    return (
        unique,
        f"ok: {len(unique)} items across {len(subreddits) - failures}/{len(subreddits)} subs",
    )
