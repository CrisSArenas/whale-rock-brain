"""GitHub ingestion via the public REST API.

For each ticker we pull two streams:
  1. Recent activity from the company's own org(s) — newest issues + releases
     across their top repos. Captures release cadence, customer pain points
     filed publicly, and security disclosures.
  2. A global issue search for the company name — third-party repos that
     mention the product (integrations, problems, migrations away).

Works without a token (60 req/h shared across the host); a ``GITHUB_TOKEN`` in
``.env`` raises this to 5000/h. We never raise on rate limits — we tag the
status and move on.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import httpx

from datetime import datetime as _dt, timedelta as _td

from ..config import settings
from ..observability import log
from ..schemas import SourceItem, TimeWindow, WINDOW_DAYS


GH_BASE = "https://api.github.com"
TIMEOUT = 12.0


def _short_id(raw: str) -> str:
    return f"G-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:6]}"


def _headers() -> dict[str, str]:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "whale-rock-brain/0.1",
    }
    token = settings.github_token.get_secret_value()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


async def _list_org_repos(client: httpx.AsyncClient, org: str, top_n: int = 3) -> list[dict[str, Any]]:
    try:
        r = await client.get(
            f"{GH_BASE}/orgs/{org}/repos",
            params={"sort": "updated", "per_page": str(top_n)},
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []


async def _repo_recent_issues(
    client: httpx.AsyncClient, full_name: str, per_page: int, since_iso: str | None = None
) -> list[dict[str, Any]]:
    params = {"state": "all", "sort": "updated", "per_page": str(per_page)}
    if since_iso:
        params["since"] = since_iso
    try:
        r = await client.get(
            f"{GH_BASE}/repos/{full_name}/issues",
            params=params,
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        # Filter pull requests — they appear in the issues endpoint.
        return [i for i in r.json() if "pull_request" not in i]
    except Exception:
        return []


async def _repo_recent_releases(
    client: httpx.AsyncClient, full_name: str, per_page: int = 3
) -> list[dict[str, Any]]:
    try:
        r = await client.get(
            f"{GH_BASE}/repos/{full_name}/releases",
            params={"per_page": str(per_page)},
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []


async def _global_issue_search(
    client: httpx.AsyncClient, keyword: str, per_page: int
) -> list[dict[str, Any]]:
    try:
        r = await client.get(
            f"{GH_BASE}/search/issues",
            params={"q": f"{keyword} in:title,body sort:updated-desc", "per_page": str(per_page)},
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        return r.json().get("items", [])
    except Exception:
        return []


def _issue_to_item(issue: dict[str, Any], repo_full: str | None = None) -> SourceItem:
    repo_full = repo_full or issue.get("repository_url", "").rsplit("/", 2)[-2:]
    repo_full = repo_full if isinstance(repo_full, str) else "/".join(repo_full)
    number = issue.get("number")
    title = (issue.get("title") or "").strip()
    body = (issue.get("body") or "").strip()
    url = issue.get("html_url") or ""
    user = (issue.get("user") or {}).get("login") or "unknown"
    state = issue.get("state") or "unknown"
    comments = issue.get("comments") or 0
    created = _parse_dt(issue.get("created_at"))
    updated = _parse_dt(issue.get("updated_at"))

    header = f"[github {repo_full}#{number} · issue · {state} · {comments} comments]"
    raw_text = f"{header}\n{title}\n\n{body[:1500]}".strip()
    return SourceItem(
        id=_short_id(f"{repo_full}#{number}"),
        source="github",
        title=title or f"{repo_full}#{number}",
        url=url,
        author=user,
        published_at=updated or created,
        raw_text=raw_text,
        metadata={"repo": repo_full, "issue_number": number, "state": state, "kind": "issue", "comments": comments},
    )


def _release_to_item(release: dict[str, Any], repo_full: str) -> SourceItem:
    name = release.get("name") or release.get("tag_name") or ""
    body = (release.get("body") or "").strip()
    url = release.get("html_url") or ""
    user = (release.get("author") or {}).get("login") or "unknown"
    published = _parse_dt(release.get("published_at"))
    tag = release.get("tag_name") or ""

    header = f"[github {repo_full} · release · {tag}]"
    raw_text = f"{header}\n{name}\n\n{body[:1500]}".strip()
    return SourceItem(
        id=_short_id(f"{repo_full}@{tag}"),
        source="github",
        title=f"{repo_full} {name or tag}",
        url=url,
        author=user,
        published_at=published,
        raw_text=raw_text,
        metadata={"repo": repo_full, "tag": tag, "kind": "release"},
    )


async def fetch(
    ticker_meta: dict[str, Any], time_window: TimeWindow = "1month"
) -> tuple[list[SourceItem], str]:
    orgs: list[str] = ticker_meta.get("github_orgs") or []
    keywords: list[str] = ticker_meta.get("github_keywords") or []
    per_repo = max(2, settings.github_items // 4)
    days = WINDOW_DAYS.get(time_window, 30)
    since_iso = (_dt.utcnow() - _td(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    items: list[SourceItem] = []
    rate_limited = False

    async with httpx.AsyncClient() as client:
        # 1) For each org, walk top-3 repos and grab recent issues + releases.
        for org in orgs[:2]:
            repos = await _list_org_repos(client, org, top_n=3)
            if not repos:
                rate_limited = True
                continue
            for repo in repos:
                full = repo.get("full_name")
                if not full:
                    continue
                issues = await _repo_recent_issues(client, full, per_page=per_repo, since_iso=since_iso)
                for iss in issues:
                    items.append(_issue_to_item(iss, repo_full=full))
                releases = await _repo_recent_releases(client, full, per_page=2)
                for rel in releases:
                    items.append(_release_to_item(rel, full))

        # 2) Global issue search for the most distinctive keyword (third-party repos).
        for kw in keywords[:1]:
            global_hits = await _global_issue_search(client, kw, per_page=settings.github_items)
            if not global_hits:
                continue
            for hit in global_hits:
                # repository_url ends with /repos/{owner}/{name}
                rurl = hit.get("repository_url") or ""
                repo_full = "/".join(rurl.split("/")[-2:]) if rurl else ""
                items.append(_issue_to_item(hit, repo_full=repo_full))

    # Dedupe by id.
    seen: set[str] = set()
    unique: list[SourceItem] = []
    for it in items:
        if it.id in seen:
            continue
        seen.add(it.id)
        unique.append(it)

    # Pre-filter: drop closed issues with no comments and no body — they're
    # near-certainly noise (auto-closed bot reports, accidental opens). Keep
    # all releases regardless because each is signal.
    def _is_substantive(it: SourceItem) -> bool:
        if it.metadata.get("kind") == "release":
            return True
        comments = it.metadata.get("comments") if isinstance(it.metadata.get("comments"), int) else None
        body_len = len((it.raw_text or "")) - len(it.title or "")
        # If the issue is closed AND has 0 comments AND the body is empty, drop it.
        if it.metadata.get("state") == "closed" and (comments == 0 or comments is None) and body_len < 80:
            return False
        return True

    unique = [it for it in unique if _is_substantive(it)]

    # Sort newest first, cap at the configured limit.
    unique.sort(key=lambda i: i.published_at or datetime.min, reverse=True)
    unique = unique[: max(settings.github_items, 5)]

    if not unique:
        if rate_limited:
            log.warning("github.rate_limited")
            return [], "rate-limited (set GITHUB_TOKEN to raise the limit)"
        return [], "ok: 0 items"
    return unique, f"ok: {len(unique)} items"
