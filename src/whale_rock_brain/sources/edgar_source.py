"""SEC EDGAR ingestion via the public submissions JSON API.

EDGAR is the *grounding* source: it gives the Brain an authoritative anchor
to compare softer signals against. Beyond just listing filings, this module
fetches the primary document for each filing and extracts the substantive
content the LLM needs to produce a useful summary:

  * 10-Q and 10-K: locate "Management's Discussion and Analysis" and pull
    a 5K-character window starting from that section. Falls back to the
    first 3K characters of the cleaned document if the heading isn't found.
  * 8-K: take the first 4K characters, which usually covers the disclosed
    items and any attached press release content.
  * Form 4 (insider transactions): take the first 1.5K characters of the
    largely-tabular document.
  * Other relevant forms: first 2.5K characters.

SEC requires a contact email in the User-Agent string per their fair-access
policy (set ``EDGAR_CONTACT_EMAIL`` in ``.env``). We fetch the company tickers
mapping dynamically so no CIKs are hardcoded.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from typing import Any

import httpx

from ..config import settings
from ..observability import log
from ..schemas import SourceItem, TimeWindow


SEC_BASE = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
TICKER_MAP_URL = f"{SEC_BASE}/files/company_tickers.json"
TIMEOUT = 12.0
DOC_TIMEOUT = 20.0
DOC_CONCURRENCY = 3  # SEC asks for <= 10 req/s; this stays well inside


def _short_id(raw: str) -> str:
    return f"E-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:6]}"


def _headers(accept: str = "application/json") -> dict[str, str]:
    return {
        "User-Agent": f"WhaleRockBrain/0.1 ({settings.edgar_contact_email})",
        "Accept": accept,
    }


_TICKER_CIK_CACHE: dict[str, str] = {}


async def _resolve_cik(client: httpx.AsyncClient, ticker: str) -> str | None:
    if ticker in _TICKER_CIK_CACHE:
        return _TICKER_CIK_CACHE[ticker]
    try:
        r = await client.get(TICKER_MAP_URL, headers=_headers(), timeout=TIMEOUT)
        if r.status_code != 200:
            log.warning("edgar.ticker_map_http", status=r.status_code)
            return None
        data = r.json()
    except Exception as exc:
        log.warning("edgar.ticker_map_failed", error=str(exc))
        return None
    target = ticker.upper()
    for entry in data.values():
        if entry.get("ticker", "").upper() == target:
            cik_int = entry.get("cik_str")
            if isinstance(cik_int, int):
                cik = str(cik_int).zfill(10)
                _TICKER_CIK_CACHE[ticker] = cik
                return cik
    return None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# --- HTML cleaning + section extraction --------------------------------------

_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")

# Common HTML entities we encounter in EDGAR HTML filings.
_ENTITIES = {
    "&nbsp;": " ", "&#160;": " ",
    "&amp;": "&", "&#38;": "&",
    "&lt;": "<", "&#60;": "<",
    "&gt;": ">", "&#62;": ">",
    "&quot;": '"', "&#34;": '"',
    "&apos;": "'", "&#39;": "'",
    "&#8217;": "'", "&#8216;": "'",
    "&#8220;": '"', "&#8221;": '"',
    "&#8211;": "-", "&#8212;": "-",
    "&#176;": " degrees ",
}

_MDA_RE = re.compile(
    r"management['’]?s?\s+discussion\s+and\s+analysis",
    re.IGNORECASE,
)


def _strip_html(html: str) -> str:
    """Convert filing HTML into clean readable text."""
    if not html:
        return ""
    # Drop script/style blocks entirely.
    cleaned = _SCRIPT_RE.sub(" ", html)
    # Convert paragraph and line breaks into newlines so we keep some structure.
    cleaned = re.sub(r"</p\s*>", "\n\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</(div|h[1-6]|tr|li)\s*>", "\n", cleaned, flags=re.IGNORECASE)
    # Strip remaining tags.
    text = _TAG_RE.sub(" ", cleaned)
    # Decode common entities.
    for ent, repl in _ENTITIES.items():
        text = text.replace(ent, repl)
    # Decode any remaining numeric entities defensively.
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x10000 else " ", text)
    # Collapse whitespace but preserve paragraph breaks.
    text = _WS_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


def _extract_relevant_section(text: str, form: str) -> str:
    """Return the most useful slice of the cleaned filing text for this form."""
    if not text:
        return ""

    # Quarterly / annual reports: locate MD&A section if possible.
    # The phrase typically appears twice in the filing - once in the table of
    # contents and once at the actual section header. Prefer the second
    # occurrence (the real section). If only one is present, use it.
    if form in ("10-Q", "10-K"):
        matches = list(_MDA_RE.finditer(text))
        if len(matches) >= 2:
            start = matches[1].start()
            return text[start:start + 5000]
        if matches:
            return text[matches[0].start():matches[0].start() + 5000]
        # Fallback: skip the cover page / table of contents and take a big chunk.
        return text[:3500]

    # 8-K (current report). These are usually short, take a big window.
    if form == "8-K":
        return text[:4000]

    # Form 4 (insider transactions) is mostly tabular; metadata + a small chunk.
    if form == "4":
        return text[:1500]

    # Other forms (S-1, DEF 14A, 13F-HR, SC 13D/G).
    return text[:2500]


async def _fetch_filing_excerpt(
    client: httpx.AsyncClient, view_url: str, form: str
) -> str:
    """Fetch a filing document and return a clean excerpt of the most
    useful section for an analyst. Returns empty string on any failure."""
    try:
        r = await client.get(
            view_url,
            headers=_headers(accept="text/html,application/xhtml+xml,*/*"),
            timeout=DOC_TIMEOUT,
        )
        if r.status_code != 200:
            log.warning("edgar.doc_http", url=view_url, status=r.status_code)
            return ""
        text = _strip_html(r.text)
    except Exception as exc:
        log.warning("edgar.doc_failed", url=view_url, error=str(exc))
        return ""

    return _extract_relevant_section(text, form).strip()


# --- Top-level fetch ---------------------------------------------------------


async def fetch(
    ticker_meta: dict[str, Any],
    ticker: str,
    time_window: TimeWindow = "1month",  # ignored - EDGAR always returns latest
) -> tuple[list[SourceItem], str]:
    async with httpx.AsyncClient() as client:
        cik = await _resolve_cik(client, ticker)
        if not cik:
            return [], f"failed: could not resolve CIK for {ticker}"

        url = f"{SEC_DATA}/submissions/CIK{cik}.json"
        try:
            r = await client.get(url, headers=_headers(), timeout=TIMEOUT)
            if r.status_code != 200:
                log.warning("edgar.submissions_http", status=r.status_code, ticker=ticker)
                return [], f"failed: HTTP {r.status_code}"
            data = r.json()
        except Exception as exc:
            log.warning("edgar.submissions_failed", error=str(exc), ticker=ticker)
            return [], f"failed: {exc}"

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accs = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])
        primary_descs = recent.get("primaryDocDescription", [])
        n = min(len(forms), len(accs), len(dates), len(primary_docs))
        if n == 0:
            return [], "ok: 0 filings"

        keep_forms = {"8-K", "10-K", "10-Q", "DEF 14A", "S-1", "424B4", "4", "13F-HR", "SC 13D", "SC 13G"}

        # Stage 1 - collect filing metadata for forms we care about.
        candidates: list[dict[str, Any]] = []
        for i in range(n):
            form = forms[i]
            if form not in keep_forms:
                continue
            acc = accs[i]
            date = dates[i]
            doc = primary_docs[i]
            desc = primary_descs[i] if i < len(primary_descs) else ""
            acc_no_dashes = acc.replace("-", "")
            view_url = f"{SEC_BASE}/Archives/edgar/data/{int(cik)}/{acc_no_dashes}/{doc}"
            candidates.append({
                "form": form, "acc": acc, "date": date, "doc": doc,
                "desc": desc, "view_url": view_url,
            })
            if len(candidates) >= settings.edgar_items:
                break

        if not candidates:
            return [], "ok: 0 relevant filings"

        # Stage 2 - fetch the primary document for each filing concurrently,
        # bounded by a small semaphore to stay polite with SEC.
        sem = asyncio.Semaphore(DOC_CONCURRENCY)

        async def _fetch_one(meta: dict[str, Any]) -> str:
            async with sem:
                return await _fetch_filing_excerpt(client, meta["view_url"], meta["form"])

        excerpts = await asyncio.gather(*[_fetch_one(c) for c in candidates])

    # Stage 3 - build SourceItems with both metadata and the excerpt.
    items: list[SourceItem] = []
    excerpts_with_content = 0
    for meta, excerpt in zip(candidates, excerpts):
        form, acc, date, desc, view_url = (
            meta["form"], meta["acc"], meta["date"], meta["desc"], meta["view_url"],
        )
        if excerpt:
            excerpts_with_content += 1

        title = f"{form} filing - {desc}".strip(" -") if desc else f"{form} filing"
        header = (
            f"[SEC EDGAR | {form} | filed {date}]\n"
            f"Company: {ticker_meta.get('company_name', ticker)} (CIK {cik}). {desc}\n"
            f"Filing accession: {acc}.\n"
        )
        if excerpt:
            section_label = (
                "MANAGEMENT DISCUSSION & ANALYSIS (excerpt)" if form in ("10-Q", "10-K") and _MDA_RE.search(excerpt)
                else f"FILING EXCERPT ({form})"
            )
            raw_text = f"{header}\n--- {section_label} ---\n{excerpt}"
        else:
            raw_text = (
                f"{header}\n"
                "Could not fetch document content; metadata only. "
                "The original filing is available at the URL."
            )

        items.append(
            SourceItem(
                id=_short_id(acc),
                source="edgar",
                title=title,
                url=view_url,
                author=ticker_meta.get("company_name") or ticker,
                published_at=_parse_date(date),
                raw_text=raw_text,
                metadata={
                    "form": form, "accession": acc, "filing_date": date, "cik": cik,
                    "has_content": bool(excerpt),
                    "excerpt_chars": len(excerpt) if excerpt else 0,
                },
            )
        )

    if not items:
        return [], "ok: 0 relevant filings"
    content_note = f", {excerpts_with_content} with full content" if excerpts_with_content < len(items) else ""
    return items, f"ok: {len(items)} filings{content_note}"
