"""X / Twitter ingestion via the v2 ``/tweets/search/recent`` endpoint.

Requires a paid X API tier. Set ``X_BEARER_TOKEN`` in ``.env``. Without it,
this source returns an empty list with a clear status — the pipeline
continues over the other sources.

The query is built from the ticker's ``aliases`` plus a cashtag (``$AMD``)
filter. We exclude retweets, restrict to English, and pull the most recent
results sorted by recency.

Selectivity matters more than volume here. We over-fetch (up to 50 tweets)
and then aggressively filter out FOMO / pump / affiliate-spam content via
text-pattern analysis BEFORE sending anything to the LLM. Verified or
large-following authors get a bypass — a real analyst saying "$200 target"
is signal, the same phrase from a 200-follower account is noise.

Tier guidance:
- **Basic** ($200/month) — 10K tweets/month, recent-search up to 7 days, 1
  app, 1500 reads/15min. Sufficient for 5-10 tickers refreshed hourly.
- **Pro** ($5K/month) — full archive search, 1M tweets/month. Required for
  serious back-testing.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

import httpx

from ..config import settings
from ..observability import log
from ..schemas import SourceItem, TimeWindow, WINDOW_DAYS


X_RECENT_SEARCH = "https://api.twitter.com/2/tweets/search/recent"
TIMEOUT = 12.0
# Pull deeper than we'll show — the FOMO filter removes a meaningful fraction.
MAX_RESULTS_FETCH = 50
AUTHORITY_FOLLOWER_THRESHOLD = 50_000


# ---------- FOMO / pump-noise detector ----------
#
# These are tweets that should never reach an analyst's eye: pure pump posts,
# affiliate spam, all-caps "to the moon" hype, or DM-me-for-signals scams.
# We're conservative — patterns must be high-precision to avoid dropping real
# analyst commentary that happens to share a vocabulary.

_FOMO_PATTERNS = [
    re.compile(r"\bto\s+the\s+moon\b", re.IGNORECASE),
    re.compile(r"\b(buy|long|short)\s+(now|today|asap|tomorrow)\b", re.IGNORECASE),
    re.compile(r"\b(dm|message|pm)\s+me\b", re.IGNORECASE),
    re.compile(
        r"\b(join|follow)\s+my\s+(discord|telegram|signals|group|channel|server)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bcheck\s+(my|the)\s+(bio|profile|link\s+in\s+bio)\b", re.IGNORECASE),
    re.compile(
        r"\b(free\s+signals|free\s+picks|trade\s+signals|trading\s+signals|free\s+entry)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bnot\s+financial\s+advice\b", re.IGNORECASE),
    re.compile(r"\b(call|put)s?\s+printing\b", re.IGNORECASE),
    re.compile(r"\b(easy|free)\s+money\b", re.IGNORECASE),
    re.compile(r"\b(load|loaded)\s+up\b", re.IGNORECASE),
    re.compile(r"\bbagholder|bag\s+holder\b", re.IGNORECASE),
    re.compile(r"\b(huge|massive|insane)\s+gains?\b", re.IGNORECASE),
    # Rocket / diamond / moon emoji spam
    re.compile(r"🚀{2,}|💎{2,}|🌙{2,}|💰{2,}|🤑"),
    # "AMD $200 EOY" pump pattern
    re.compile(r"\$\s*\d{2,5}\s+(?:eoy|target|incoming|next|by\s+\w+)", re.IGNORECASE),
]


def _is_fomo_noise(text: str, follower_count: int = 0) -> bool:
    """Return True if the tweet looks like FOMO / pump / spam noise.

    Verified or large-following authors get a bypass on this filter — a
    serious analyst account using "to the moon" sarcastically still has
    follower-weighted reach.
    """
    if not text:
        return True
    if follower_count >= AUTHORITY_FOLLOWER_THRESHOLD:
        return False

    for pat in _FOMO_PATTERNS:
        if pat.search(text):
            return True

    # Excessive exclamation marks — a hallmark of pump tweets.
    if text.count("!") >= 4:
        return True

    # Mostly-uppercase body (PUMP STYLE), excluding tickers and short tweets.
    letters = re.sub(r"[^A-Za-z]", "", text)
    if len(letters) > 40:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.55:
            return True

    return False



def _short_id(raw: str) -> str:
    return f"X-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:6]}"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _build_query(ticker: str, aliases: list[str]) -> str:
    """Build a recent-search query that's tight enough to avoid noise.

    Uses cashtag for the ticker plus quoted full-name aliases. Excludes
    retweets and restricts to English to keep the population on-topic.
    """
    pieces: list[str] = [f"${ticker.upper()}"]
    # Skip the bare symbol if it's already the cashtag.
    quoted = [f'"{a}"' for a in aliases if a.upper() != ticker.upper()][:3]
    if quoted:
        pieces.append("(" + " OR ".join(quoted) + ")")
    pieces_str = "(" + " OR ".join(pieces) + ")"
    return f"{pieces_str} -is:retweet lang:en"


async def fetch(
    ticker: str,
    ticker_meta: dict[str, Any],
    time_window: TimeWindow = "1month",
) -> tuple[list[SourceItem], str]:
    bearer = settings.x_bearer_token.get_secret_value()
    if not bearer:
        return [], "skipped: X_BEARER_TOKEN not set (see README to enable)"

    aliases = ticker_meta.get("aliases") or []
    query = _build_query(ticker, aliases)
    # Recent-search supports a 7-day window on Basic. We send start_time but X
    # silently caps it; the status string flags this for the user.
    days = min(WINDOW_DAYS.get(time_window, 30), 7)
    start_time = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "query": query,
        "max_results": str(min(MAX_RESULTS_FETCH, 100)),
        "start_time": start_time,
        "tweet.fields": "created_at,public_metrics,author_id,lang",
        "expansions": "author_id",
        "user.fields": "username,name,public_metrics,verified",
    }
    headers = {
        "Authorization": f"Bearer {bearer}",
        "User-Agent": "whale-rock-brain/0.1",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
            r = await client.get(X_RECENT_SEARCH, params=params)
            if r.status_code == 429:
                return [], "rate-limited (X recent-search 15-min cap)"
            if r.status_code in (401, 403):
                log.warning("x.auth_error", status=r.status_code)
                return [], f"auth: HTTP {r.status_code} (check X_BEARER_TOKEN tier and validity)"
            if r.status_code != 200:
                log.warning("x.http_error", status=r.status_code)
                return [], f"failed: HTTP {r.status_code}"
            data = r.json()
    except Exception as exc:
        log.warning("x.fetch_failed", error=str(exc))
        return [], f"failed: {exc}"

    tweets = data.get("data") or []
    users = {u["id"]: u for u in (data.get("includes") or {}).get("users", [])}

    if not tweets:
        return [], "ok: 0 tweets"

    items: list[SourceItem] = []
    fomo_dropped = 0
    low_engagement_dropped = 0
    for t in tweets:
        tid = t.get("id")
        text = (t.get("text") or "").strip()
        if not tid or not text:
            continue
        author_id = t.get("author_id")
        author = users.get(author_id, {})
        username = author.get("username") or "unknown"
        verified = author.get("verified")
        followers = (author.get("public_metrics") or {}).get("followers_count") or 0
        published = _parse_dt(t.get("created_at"))
        metrics = t.get("public_metrics") or {}
        likes = metrics.get("like_count", 0)
        rts = metrics.get("retweet_count", 0)
        replies = metrics.get("reply_count", 0)

        engagement = likes + rts + replies
        is_authority = bool(verified) or followers >= 5000

        # Filter 1 — engagement floor for non-authority accounts.
        if not is_authority and engagement < 3:
            low_engagement_dropped += 1
            continue

        # Filter 2 — FOMO / pump / affiliate-spam pattern detector. Authority
        # accounts (verified / 50K+ followers) bypass this filter.
        if _is_fomo_noise(text, followers):
            fomo_dropped += 1
            continue
        url = f"https://x.com/{username}/status/{tid}"

        meta_bits = [
            f"{likes} likes",
            f"{rts} RTs",
            f"{replies} replies",
        ]
        if followers is not None:
            meta_bits.append(f"author followers: {followers:,}")
        if verified:
            meta_bits.append("verified")
        header = f"[X · @{username} · " + " · ".join(meta_bits) + "]"
        raw_text = f"{header}\n{text}".strip()

        items.append(
            SourceItem(
                id=_short_id(f"x-{tid}"),
                source="x",
                title=text[:140] if len(text) > 140 else text,
                url=url,
                author=username,
                published_at=published,
                raw_text=raw_text,
                metadata={
                    "tweet_id": tid,
                    "username": username,
                    "verified": verified,
                    "likes": likes,
                    "retweets": rts,
                    "replies": replies,
                    "author_followers": followers,
                },
            )
        )

    requested = WINDOW_DAYS.get(time_window, 30)
    cap_note = f" (recent-search capped at 7d; requested {requested}d)" if requested > 7 else ""
    filter_note = ""
    if fomo_dropped or low_engagement_dropped:
        bits = []
        if fomo_dropped:
            bits.append(f"{fomo_dropped} FOMO/pump")
        if low_engagement_dropped:
            bits.append(f"{low_engagement_dropped} low-engagement")
        filter_note = f" [filtered {', '.join(bits)}]"
    return items, f"ok: {len(items)} tweets{filter_note}{cap_note}"
