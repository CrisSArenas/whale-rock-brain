"""Industry news ingestion via the Google News RSS endpoint.

Google News exposes a public RSS feed that aggregates results across thousands
of publishers — including the niche industry trade rags the case study brief
specifically mentions ("a niche industry podcast where an ex-employee says the
quiet part out loud"). We pass a per-ticker query that biases toward the
company name plus substantive keywords, parse the RSS XML with the standard
library, and return one item per article.

No auth, no key, no rate limit advertised. We send a polite User-Agent and
keep the per-ticker fetch volume modest.
"""
from __future__ import annotations

import hashlib
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from datetime import timedelta

from ..observability import log
from ..schemas import SourceItem, TimeWindow, WINDOW_DAYS


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
USER_AGENT = "whale-rock-brain/0.1 (research)"
TIMEOUT = 10.0
MAX_ITEMS = 12


def _short_id(raw: str) -> str:
    return f"N-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:6]}"


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _TAG_RE.sub(" ", s).replace("&nbsp;", " ").replace("&amp;", "&").strip()


def _parse_pubdate(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        return dt.replace(tzinfo=None) if dt else None
    except (TypeError, ValueError):
        return None


async def fetch(
    ticker: str,
    ticker_meta: dict[str, Any],
    time_window: TimeWindow = "1month",
) -> tuple[list[SourceItem], str]:
    query = ticker_meta.get("news_query") or ticker_meta.get("company_name") or ticker
    # Google News RSS doesn't honor `when:` filters reliably, so we filter
    # client-side by pubDate against the requested window.
    days = WINDOW_DAYS.get(time_window, 30)
    cutoff = datetime.utcnow() - timedelta(days=days)
    params = {
        "q": query,
        "hl": "en-US",
        "gl": "US",
        "ceid": "US:en",
    }
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"}

    url = f"{GOOGLE_NEWS_RSS}?{urllib.parse.urlencode(params)}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                log.warning("news.http_error", ticker=ticker, status=r.status_code)
                return [], f"failed: HTTP {r.status_code}"
            xml_text = r.text
    except Exception as exc:
        log.warning("news.fetch_failed", ticker=ticker, error=str(exc))
        return [], f"failed: {exc}"

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("news.parse_failed", ticker=ticker, error=str(exc))
        return [], f"failed: parse error"

    items: list[SourceItem] = []
    seen_ids: set[str] = set()

    for entry in root.findall(".//item"):
        title = _strip_html((entry.findtext("title") or "").strip())
        link = (entry.findtext("link") or "").strip()
        pub_raw = entry.findtext("pubDate")
        descr = _strip_html((entry.findtext("description") or "").strip())
        # Google News tags the publisher in <source> with a name.
        src_el = entry.find("source")
        publisher = (src_el.text or "").strip() if src_el is not None and src_el.text else ""
        guid = (entry.findtext("guid") or link or title).strip()
        if not title or not link:
            continue
        item_id = _short_id(guid)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        published = _parse_pubdate(pub_raw)
        # Apply window filter (skip articles older than the cutoff). If we
        # can't parse the pubDate at all, keep the item rather than drop it.
        if published is not None and published < cutoff:
            continue
        date_str = published.strftime("%b %d, %Y") if published else ""
        header_bits = [f"[Google News"]
        if publisher:
            header_bits.append(f"publisher: {publisher}")
        if date_str:
            header_bits.append(date_str)
        header = " · ".join(header_bits) + "]"
        raw_text = f"{header}\n{title}\n\n{descr[:1200]}".strip()

        items.append(
            SourceItem(
                id=item_id,
                source="news",
                title=title,
                url=link,
                author=publisher or "news",
                published_at=published,
                raw_text=raw_text,
                metadata={"publisher": publisher, "kind": "news"},
            )
        )
        if len(items) >= MAX_ITEMS:
            break

    if not items:
        return [], "ok: 0 articles"
    return items, f"ok: {len(items)} articles"
