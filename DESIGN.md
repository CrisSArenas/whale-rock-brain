# Whale Rock Brain, Design Document

This is the design document for the Whale Rock Brain. It covers the rubric questions in the case study brief, plus the questions a technical reviewer is likely to ask in a code review. Each section explains what was built, what was considered and rejected, and why.

---

## Table of contents

1. [Source selection](#1-source-selection)
2. [System architecture](#2-system-architecture)
3. [The storage and state model](#3-the-storage-and-state-model)
4. [The Brain Summary structure, and why these sections](#4-the-brain-summary-structure-and-why-these-sections)
5. [Hallucination defenses](#5-hallucination-defenses)
6. [Noise defenses, distinct from hallucination](#6-noise-defenses-distinct-from-hallucination)
7. [The connections engine](#7-the-connections-engine)
8. [Concurrency, rate limits, and failure modes](#8-concurrency-rate-limits-and-failure-modes)
9. [Prompt engineering choices](#9-prompt-engineering-choices)
10. [Evaluation, both built and planned](#10-evaluation-both-built-and-planned)
11. [Cost and latency](#11-cost-and-latency)
12. [Per-ticker fan-out, and the path from 9 to 200 tickers](#12-per-ticker-fan-out-and-the-path-from-9-to-200-tickers)
13. [Risks](#13-risks)
14. [Roadmap](#14-roadmap)
15. [Build vs. buy](#15-build-vs-buy)

---

## 1. Source selection

The brief is explicit: three sources done well beats eight done badly. The system runs six sources, picked deliberately, with explicit rejections for everything else considered. The display order in the dashboard is Reddit, Industry News, GitHub, Hacker News, X, and SEC EDGAR.

### The six sources, with rationale

**Reddit.** Subreddit-aware search across stock subs and product/operator subs. The relevant subreddits are configured per ticker in `data/tickers.json`. For AMD that means r/AMD_Stock and r/buildapc. For FROG it's r/devops and r/kubernetes. For KVYO it's r/shopify and r/ecommerce. The reason we configure subs per ticker rather than searching globally is precision. A global Reddit search for "AMD" pulls anti-money-laundering threads, alcohol and music discussions, and a hundred other false positives. Constraining to known-relevant subs trades a bit of recall for a lot of precision, which is the right trade for a buy-side dashboard where false positives are more costly than missed coverage.

The Reddit endpoint we use is the public `.json` API. We send a browser-class User-Agent string, because Reddit fingerprints generic Python clients and returns 403. We pace the per-subreddit calls 600 milliseconds apart, because the unauthenticated tier is rate limited. We also drop posts at the source level if they have no comments, no upvote score, and an empty body, because those are almost always automod-stuff or noise.

**Industry News.** Aggregated trade publications via Google News' free RSS endpoint. The brief specifically calls out "niche industry rags" as a target source, and Google News' free aggregation surfaces exactly those: SemiAnalysis-class technical blogs, channel-specific coverage, foreign press, and trade publications that Bloomberg buckets together as "Other." The query is per-ticker (defined in `data/tickers.json`) and returns a 60+ item RSS feed. We filter client-side by `pubDate` against the requested time window because Google News' `when:` operator returned zero results in testing, while a plain query without `when:` returned 60. We learned this empirically and switched to client-side filtering.

**GitHub.** Recent issues and releases for the company's top repos, plus a global issue search for product mentions in third-party repositories. This is the operational signal that's hardest to fake. Companies cannot hide their release cadence, their public bug reports, or the fact that Big Customer Co just opened an issue against their SDK. For tech-heavy names (FROG via jfrog/artifactory, KVYO via klaviyo SDKs, AMD via ROCm, MDB via mongodb) this captures the build/operate signal that leads enterprise revenue by 2 to 3 quarters. An optional `GITHUB_TOKEN` raises the rate limit from 60 per hour to 5,000 per hour. We drop closed issues with zero comments and an empty body because those are bot-closed staleness.

**Hacker News.** The Algolia public search API, full-text across stories and comments. HN is where TMT narratives crystallize early. Founder commentary, infrastructure post-mortems, "we just migrated from X to Y" threads. The valuable and non-obvious thing about HN is timing: a thoughtful HN top-comment about a product is often two or three quarters ahead of when the same opinion shows up in a sell-side note.

There's an implementation gotcha with HN's Algolia API that took some debugging. The `query` parameter does not support boolean OR. If you send `query="JFrog" OR "Artifactory"`, Algolia treats `OR` as a literal word and AND-matches every term, returning zero hits. The fix is to use `query=JFrog` (the most distinctive single term) and pass `optionalWords=Artifactory FROG` as a separate parameter, which lets Algolia treat the additional aliases as boost terms but not requirements. We discovered this empirically.

**X (Twitter).** The v2 `/tweets/search/recent` endpoint. This is the only paid source, because X has no free tier suitable for the volume this needs. Basic at $200 per month gets 10,000 tweets monthly, which covers about 10 tickers refreshed hourly. Pro at $5,000 per month gets the full archive. Outside the case study budget, but the integration is built and ready: set `X_BEARER_TOKEN` and the column populates.

The valuable thing about X is real-time sentiment with author weighting. A tweet from a 200,000-follower analyst account is qualitatively different from a tweet from a 50-follower meme account, even if they say the same words. We pull up to 50 tweets per refresh and run a regex-based pre-filter that drops obvious FOMO and pump content (more on that in Section 6) before any LLM call. Verified or 50K-plus follower accounts bypass the filter, because a serious analyst saying "$200 target" is signal even if it shares vocabulary with retail noise.

**SEC EDGAR.** The submissions JSON API. Recent 8-K, 10-Q, 10-K, Form 4, and 13F filings. The CIK is resolved dynamically from `company_tickers.json` so we don't hardcode CIKs. EDGAR plays a different role from the other five sources: it's the authoritative anchor. Every other source is unstructured opinion from someone with an agenda. EDGAR is what the company actually told the SEC. The Brain Summary is built to compare softer signals against this anchor. Critically, EDGAR ignores the user's time window setting and always returns the most recent filings. The reasoning: the analyst always wants the latest 10-Q, regardless of whether they're studying the last month or the last year of alt-data.

### What we considered and rejected

**Twitter (free tier).** No free tier suitable for this. X's pricing forces the choice. Listed in the roadmap once a license is paid for.

**Glassdoor and Levels.fyi.** High-signal sources for catching sales reorgs, layoffs, and compensation shifts. No public API. Scraping them violates ToS. To use these properly in production would require licensing a vendor feed (Revelio Labs, Thinknum, Linkup). Costs are in the high four-figures monthly. Listed as the highest-priority roadmap item because the alpha is real.

**App Store and Play Store reviews.** Apple's RSS endpoint was deprecated. Google Play has no public reviews API. Both require licensed data wrappers (Sensor Tower, data.ai). Listed in the roadmap.

**Podcast transcripts.** Transcribing earnings calls and ex-employee podcasts is high signal but cost-prohibitive at request time. The right architecture is a separate batch Whisper pipeline running offline, with the transcripts stored as an additional source the orchestrator queries. Out of scope for the case study, but the architecture supports it: it would be a `sources/podcast_source.py` module that reads from a transcripts table.

**Stack Overflow.** We built it, tested it on the case study tickers, and dropped it. Stack Overflow's Q&A volume has fallen substantially as developer questions moved to LLM assistants and Discord. The signal that remains is largely duplicative of GitHub issues. The replacement was X, which captures a fundamentally different population.

**Stocktwits.** Tested. Their public stream API now returns 403 for non-authenticated requests, even with browser User-Agent strings. They've gated the public access. We could pay for it, but X covers the same population (active traders) with broader coverage, so we picked X over Stocktwits when the budget was forced.

**LinkedIn job postings.** A "company is hiring 20 customer retention specialists" signal would be valuable. LinkedIn has no public job listings API. Indeed deprecated theirs in 2023. Listed in roadmap as licensed-feed territory.

---

## 2. System architecture

The system has three layers, deliberately decoupled. Each layer has one job and can be deployed, scaled, or replaced independently.

### Layer 1, ingestion

Six source modules in `src/whale_rock_brain/sources/`. Each one exposes the same async function signature:

```python
async def fetch(ticker_meta: dict, time_window: TimeWindow) -> tuple[list[SourceItem], str]:
    ...
```

It returns a list of normalized `SourceItem` objects plus a status string the dashboard surfaces ("ok: 18 items", "rate-limited", "failed: HTTP 503"). The orchestrator runs all six concurrently using `asyncio.gather` wrapped in a `_safe_fetch` helper that catches any exception and converts it into an empty list plus an error status. This means a single source going down never breaks the others.

Each source honors a `time_window` parameter: 1 week, 1 month, 3 months, 6 months, or 1 year. Implementation differs per source:

* Reddit uses its native `t=` bucket (week, month, year).
* HN uses `numericFilters=created_at_i>cutoff` against a Unix timestamp.
* GitHub issues use `since=` with an ISO timestamp.
* X uses `start_time` (capped to 7 days for the Basic tier; we surface the cap in the status string).
* Industry News filters client-side on `pubDate` because Google News' `when:` operator is unreliable.
* EDGAR ignores the window and returns the most recent filings.

### Layer 2, synthesis

Four steps, all in-process during a refresh:

1. **Cross-refresh deduplication.** The orchestrator loads the previous snapshot for this ticker, builds a dictionary of `{item_id: existing_item}`, and for every newly-fetched item checks whether it's already in the previous snapshot. If yes, the existing summary, sentiment, relevance, and themes are reused verbatim. Only genuinely new items go to the LLM. This is the biggest cost lever in steady state, dropping per-refresh cost by roughly 70 to 80 percent.

2. **Per-item summarization.** Each new item gets one Sonnet 4.6 call. The prompt frames the model as a buy-side analyst and asks for strict JSON: a one-line summary, sentiment for the security (not the author's mood), a relevance score 1 to 10, and one to four lowercase theme tags. Concurrency is capped at 4 by an `asyncio.Semaphore` in `llm.py` to stay inside Anthropic's tier-1 rate limits. The cap is configurable for higher tiers.

3. **Brain Summary.** One Sonnet 4.6 call that synthesizes across the surviving items. The output is a structured JSON document with nine sections (covered in Section 4 of this doc). It's parsed with a Pydantic model, then run through a citation validator. On validation failure, a single repair pass runs with the specific errors fed back to the model.

4. **Connections.** A two-pass engine covered in detail in Section 7. TF-IDF cosine similarity finds candidate item pairs cheaply, then up to 12 LLM-as-judge calls decide which candidates are real connections versus keyword overlap.

### Layer 3, serving

A FastAPI app reads from `data/snapshots/{TICKER}.json`. The endpoints are:

* `GET /api/tickers` returns the list of supported tickers with their groups.
* `GET /api/dashboard/{ticker}` returns the current snapshot for a ticker, or 404 if it hasn't been refreshed yet.
* `POST /api/refresh/{ticker}?window=1month` runs the full pipeline and returns the fresh snapshot.
* `POST /api/chat` answers a question against the current snapshot.
* `GET /health` is a liveness probe.
* `GET /` serves the dashboard HTML.

The dashboard never blocks on a source pull. The only thing the read path does is open a JSON file on disk. The Refresh button is the only path that triggers ingestion.

### Why decouple at all

The brief specifically asks about this: "Source ingestion should not run inside the dashboard request path." The design satisfies this in several ways:

* `orchestrator.refresh_ticker()` is a pure async function. It can be called from the Refresh API endpoint, from a CLI script, from a Lambda handler, from a Container Apps Job on a timer, from anywhere.
* The dashboard's reads are O(open one file). They never wait on a network call to a source.
* If a source is down or rate-limited, the dashboard still shows the last known snapshot.
* If a refresh is happening when the dashboard reads, no problem: the writer is `Path.write_text` which is atomic for small files, and the reader gets either the old version or the new version, never a torn read.

In production we'd run the orchestrator on a schedule (EventBridge plus Lambda on AWS, Logic App plus Container Apps Job on Azure) and let the dashboard be a pure read-only service.

### What the dashboard never does

* It never calls a source directly. All source calls happen inside `orchestrator.refresh_ticker`, which writes to disk.
* It never calls Anthropic for routine page loads. The Brain Summary, the connections, the per-item summaries are all read from disk. Only chat questions and the refresh action call the LLM at request time.
* It never blocks waiting on anything slow.

---

## 3. The storage and state model

The state lives in one JSON file per ticker, at `data/snapshots/{TICKER}.json`. The schema is the `TickerSnapshot` Pydantic model:

```python
class TickerSnapshot(BaseModel):
    ticker: str
    company_name: str
    refreshed_at: datetime
    time_window: TimeWindow
    items: list[SourceItem]
    brain: Optional[BrainSummary]
    connections: list[Connection]
    last_run_metrics: Optional[RunMetrics]
    source_status: dict[str, str]
```

### Why JSON files instead of a database

Three reasons:

1. **The access pattern is per-ticker.** Every read is "give me everything for one ticker." Every write is "rewrite everything for one ticker." A multi-ticker database adds nothing. A flat per-ticker file is exactly the right shape.

2. **Simplicity.** No migrations, no connection pooling, no schema-versioning headache. The Pydantic model is the schema.

3. **Cloud portability.** The same shape works for S3, Blob Storage, or DynamoDB. Swapping `Path.write_text(json.dumps(...))` for `s3_client.put_object(...)` is a one-line change in `storage.py`. The orchestrator and the API don't need to know.

When would we move to a real database? Three triggers:

* **Cross-ticker queries.** "Show me every ticker where Reddit sentiment turned negative this week." That requires an indexed columnar store.
* **High concurrent write rate.** A scheduled refresh of 200 tickers all writing simultaneously is fine for files but starts to want WAL-style guarantees. DynamoDB or RDS would be more comfortable.
* **Audit history.** Right now each refresh overwrites the previous snapshot. If we need a full audit trail (who ran which refresh when, what the previous Brain said), we'd write each refresh to a versioned record.

For 9 tickers in a case study, files are right.

### What's in `SourceItem`

Each item carries:

* `id`: a short hash like `R-7f3a` for Reddit, `H-1a2b` for HN, `G-3c4d` for GitHub. The first letter encodes the source. The second part is a hash of the source-stable identifier (subreddit + post id, HN object id, GitHub repo + issue number, etc.). This means the same Reddit post produces the same id on every refresh, which is the foundation for cross-refresh deduplication.
* `source`, `title`, `url`, `author`, `published_at`, `raw_text`, and a free-form `metadata` dict per source.
* The LLM-filled fields: `summary`, `sentiment`, `relevance`, `themes`. These start as None on fetch and are filled in during the summarization pass.

### What's in `BrainSummary`

Nine fields, all required by the validator:

* `headline`, `narrative`, `whats_changing`, `bear_case`, `bull_case` (the original analytical sections).
* `position_read`, `confidence`, `confidence_rationale` (the PM-actionable sections, added in v0.2).
* `watch_list`, `management_questions` (the next-action-item sections).
* `cited_ids` (the deduped list of every source id cited anywhere, populated by the validator from the text).

### What's in `Connection`

Four fields per connection:

* `item_ids`: the two source item ids being linked.
* `theme`: the LLM's one-line description of what connects them.
* `rationale`: the LLM's two-to-three-sentence justification.
* `confidence`: Low, Medium, or High. Low connections are dropped before they're stored.
* `similarity`: the raw TF-IDF cosine score, 0 to 1, kept on the connection so the dashboard can show it for transparency.

### Why preserve everything in the snapshot, even low-relevance items

Earlier versions of the orchestrator filtered out low-relevance items at the snapshot level. We saw a real case (FROG) where 46 items were ingested and only 3 survived to the dashboard. The Brain Summary was good but the dashboard felt empty.

The current design separates two concerns:

* **What the analyst sees.** All summarized items, sorted by source then relevance descending. The analyst can scan everything that came back, including the low-relevance items, with the right amount of visual de-emphasis.
* **What the Brain synthesizes from.** The Brain prompt internally filters to relevance >= 3 (or relevance >= 1 for thin-coverage names where too few items clear the higher bar). This keeps the synthesis signal-heavy without hiding the underlying data from the analyst.

The exception: items the model flagged as `promotional` with relevance <= 2 are dropped entirely. Those are pure marketing noise and add nothing to the dashboard.

---

## 4. The Brain Summary structure, and why these sections

The Brain Summary is the headline output. It's deliberately structured around a portfolio manager who is deciding whether to add to, hold, or trim a position. Generic LLM summaries do not do this. They tend to produce a topic-by-topic recap: "AMD launched a new GPU. JFrog announced a partnership. Klaviyo's CEO spoke at a conference." That structure is useless to a PM, because it doesn't connect the dots and it doesn't suggest action.

Our structure has nine sections, in this order:

| Section | Question it answers |
|---|---|
| Headline | What's the one sentence I'd forward to my PM? |
| Position read | What does this data argue for or against? |
| Confidence | How sure are we, and why? |
| Narrative | What's the cross-source synthesis? |
| What's changing | What's different since last time? |
| What to watch | What future events would confirm or break the read? |
| Non-consensus bear | What's the bear case the sell side isn't writing? |
| Non-consensus bull | Same on the bull side? |
| Questions for management | What should I ask on the next earnings call? |

### Why no buy/sell rating

We deliberately do not produce "buy", "sell", or "hold" labels. The Position Read tells the PM what the data argues for or against, with citations, but the actual call stays with the PM. There are three reasons:

1. **We don't have the full picture.** The dashboard sees alt-data only. The PM also weighs the financial model, the macro view, position sizing, portfolio fit, and a dozen other things the system doesn't see. A "buy" rating from a system that only sees alt-data would be wrong by construction.
2. **Cost asymmetry.** A confident wrong rating costs much more than a useful read with no rating. The Brain that says "this argues against adding here, evidence is thin on the AI thesis" is much more valuable than one that says "Sell" with the same data.
3. **Regulatory caution.** A buy/sell rating from an automated system is a different category of output. Avoiding it sidesteps an entire conversation about compliance.

### Why the Position Read instead of just the narrative

We tested an earlier version with just (headline, narrative, what's changing, bear, bull). The narrative was good but the PM had to read three paragraphs to figure out what to do with it. The Position Read is the "tl;dr for the next 30 seconds": this is what the data argues for or against. It sits between the headline and the narrative in the UI so it's the second thing the PM reads.

### Why scaling Management Questions to data depth

Earlier versions always asked for exactly 5 questions. The result was that thin-coverage tickers (like SNDK, where the alt-data is genuinely sparse) got 5 generic questions ("how is the storage business?") and rich-coverage tickers (NVDA, MSFT) also got 5 because the model was asked for 5. Both buckets felt off.

The current prompt explicitly calibrates count to depth: 1 to 2 questions for thin coverage, 2 to 3 for moderate, 4 to 5 for rich. The prompt also includes good and bad examples so the model has a clear target ("What is the attach rate of Xray to Artifactory in the install base, and how has it trended Q-over-Q?" is good. "Tell us about your AI strategy" is bad).

The validator also requires at least one question to contain a citation, which forces the questions to be grounded in items rather than generic.

### What we removed and why

An earlier draft had a "key risk factors" section. We dropped it because it overlapped with the bear case. An earlier draft also had a "executive summary." We dropped it because the headline plus the position read already serve that role. The current sections are the result of three rounds of trimming.

---

## 5. Hallucination defenses

The brief calls hallucinated connections out as the number-one risk: "An LLM that confidently links unrelated source items together is worse than no Brain at all." We agree, and the system has four layers against this.

### Layer 1, closed-world prompts

Both the Brain prompt and the chat prompt include this rule, in capitalized text near the top: "You may only assert things visible in the source items I give you. If the items don't support a claim, say so explicitly. Do not infer." The chat is also instructed to return "the current data doesn't show that" when an answer isn't supported, rather than fall back to general knowledge.

This catches the easy cases. The model defaults to closed-world behavior because the prompt is unambiguous about it.

### Layer 2, citation enforcement with repair loop

The hard cases are when the model invents a citation. To catch this, every Brain output runs through `_validate_citations` in `brain.py`. The validator does two things:

1. Extracts every `[ID]` reference from every section.
2. Cross-checks each one against the actual list of valid item ids that were sent to the model.

If the model cited an id that doesn't exist (a hallucination), the validator returns a specific error: "Citations reference ids that are not in the provided items: [F-bad123]. Remove or replace them with valid ids."

The orchestrator then runs a single repair pass: it sends the original prompt plus the specific errors back to the model, with instructions to fix each error. We pass the SAME validation again. If the repair pass succeeds, we use it. If not, we keep the original and log the failure.

The validator also enforces structure: every section that requires citations (narrative, bear, bull, position read, what's-changing bullets) must have at least one [ID]. The position_read field can't be empty. The watch_list must have entries. The confidence must be one of the three allowed values. At least one management question must be grounded in a citation.

In testing, the repair loop fires on roughly 5 to 10 percent of refreshes, and resolves the issue ~95 percent of the time. The remaining ~5 percent log a clear failure mode rather than ship broken output.

### Layer 3, the connection confidence gate

Vector similarity is great at finding pairs of items that share words. It is terrible at distinguishing "real connection" from "topical adjacency." A Reddit post about "AMD GPU benchmarks" and a GitHub release of "AMD ROCm 6.4" share many words. They are not necessarily connected. The Reddit post is a benchmark comparison; the GitHub release is a software update. Same words, different topics.

Our solution is a two-pass design. The TF-IDF similarity is treated as a candidate generator: it identifies pairs that MIGHT be connected. Then each candidate is sent to Claude with this exact prompt: "Topical adjacency is NOT a connection. A Reddit post about AMD GPUs and a GitHub release of an AMD-related project are not connected unless they share a thematic thread." The judge returns a structured JSON: `is_connected: true|false`, `theme`, `rationale`, `confidence: Low|Medium|High`. The orchestrator drops any connection with `is_connected: false` or `confidence: Low`.

This works because the judge is given just two items, with focused context. It can hold both in mind and decide if they really tell a single story. Asking Claude to find connections across 30 items at once would not work; the cognitive load is too high and the false-positive rate spikes.

### Layer 4, the UI cross-check

Even after the validator and the repair loop, we belt-and-suspenders one more time on the front-end. The `linkifyCitations` function in `index.html` takes a string of text and a Set of valid ids. It walks through every `[XX-1234]` pattern and checks the id against the Set. If the id is in the Set, it renders as a clickable chip. If not, it renders as plain text.

So even if the validator missed a hallucinated id (which would be a bug), the user wouldn't be misled into clicking it.

### What we don't defend against

* **Source-level hallucination.** If the model decides to misread a Reddit post (saying "operators are leaving the platform" when the post says no such thing), that's harder to catch automatically because the source IS in the items list. We mitigate this by being strict in the per-item summarizer prompt about not fabricating numbers, names, or quotes.
* **Source content that's already false.** A Reddit post can lie. The system will faithfully report what the post said. We trust the analyst to weigh source credibility.
* **Subtle semantic drift in the Brain Summary.** The model might cite three real items and say something the items don't quite support. The repair loop and the closed-world prompt help. A future eval harness (described in Section 10) would help more.

---

## 6. Noise defenses, distinct from hallucination

Hallucination is "the model invents things." Noise is "the source returned real items that aren't worth surfacing." Both need handling, but the techniques are different.

### Promotional/press-release filter

The summarizer prompt explicitly defines pure marketing: a press release without named customers, dollar amounts, usage metrics, or executive specifics. Items meeting that definition get tagged with the theme `promotional` and capped at relevance 2. The orchestrator then drops items where `theme contains 'promotional' AND relevance <= 2` before they reach the snapshot.

This is intentional. Three press releases announcing "industry-first" features without naming a single customer don't add up to signal. They add up to a press-release stack. The Brain Summary should not build a thesis on them. By dropping them at the orchestrator level, we prevent the Brain from accidentally using them as evidence.

A press release WITH commercial substance (e.g., "Microsoft signs $5B Azure expansion with Walmart") is not promotional. It carries real signal. The summarizer prompt is explicit about this distinction with examples in-context, and we let the model judge.

### X FOMO / pump filter

X is the worst offender for noise because the platform's structure rewards engagement, and high-engagement tweets about stocks are often pumps and FOMO posts. Patterns we catch with regex before the LLM call:

* "to the moon", "calls printing", "easy money", "load up", "huge gains"
* Affiliate spam: "DM me for signals", "join my discord", "check my bio", "free signals"
* Emoji spam: 2-plus rocket, diamond, moon, or money-bag emojis in a row
* Excessive exclamation marks (4-plus per tweet)
* All-caps body (more than 55 percent uppercase letters, excluding short tweets)
* "$200 EOY" or "$200 target incoming"-style price targets without analysis

Verified accounts and accounts with 50,000-plus followers bypass this filter entirely. The reason: a real analyst with reach occasionally uses pump-style vocabulary sarcastically, or makes a serious price target call that happens to match the regex. Their reach matters even when their phrasing rhymes with retail noise.

The status string surfaces filter counts to the dashboard. After a refresh you'll see something like `ok: 14 tweets [filtered 22 FOMO/pump, 14 low-engagement]`. This way the analyst knows the system is doing the filtering, not just dropping data silently.

### Engagement floors per source

* Reddit: drop posts with 0 score, 0 comments, AND empty body. Anything else is potentially real.
* HN: drop items with 0 points, 0 comments, AND no text content.
* GitHub: drop closed issues with 0 comments AND empty body. Open issues always pass; releases always pass.
* X: covered above.

These are conservative floors. The thresholds were chosen to remove obvious noise without dropping anything that could plausibly be signal.

### Plain-English prompt rules

A separate kind of noise: LLM output that sounds professional but says nothing. Phrases like "it remains to be seen", "strategically positioned to capitalize on", "potentially significant", "could be a key driver", "in light of". The Brain prompt has an explicit forbidden-phrase list and replaces them with example concrete claims.

This isn't about preventing hallucination. It's about ensuring the output reads like a real analyst note. A PM who skim-reads a paragraph of LLM hedge-words will not trust the dashboard.

### Why this matters separately from hallucination defenses

A noisy Brain Summary that's technically accurate is still useless. If the system surveys 24 items, half of which are promotional press releases, and the Brain says "the company shipped multiple AI-related announcements," that's true. It's also worthless. A PM doesn't care about announcement count; they care about commercial traction.

The noise filters ensure the Brain has signal-rich items to work with. The hallucination defenses ensure the Brain doesn't make things up. Both are needed.

---

## 7. The connections engine

The Connections view is the differentiator from a standard alt-data feed reader. It's also the highest-risk piece, because false connections actively mislead the analyst.

### The two-pass design

**Pass 1: TF-IDF candidate generation.** For all summarized items, we build a TF-IDF matrix of `(title + summary + themes)` and compute cosine similarity for every pair. We sort the pairs by similarity descending and take the top 12 above a 0.18 cosine threshold, with same-source pairs requiring a higher threshold (cross-source pairs are intrinsically more interesting). This pass is deterministic, runs in milliseconds, and is essentially free.

**Pass 2: LLM-as-judge.** For each candidate pair, we send a focused prompt to Claude that includes only those two items. The prompt explicitly distinguishes "topical adjacency" from "real thematic connection." The model returns structured JSON: `is_connected: bool`, `theme`, `rationale`, `confidence: Low|Medium|High`. We drop everything that's not connected, and drop Low-confidence connections.

### Why two passes instead of one

Three reasons:

1. **Cost.** Pairwise judging on 30 items is 435 LLM calls (n-choose-2). At Sonnet 4.6 rates that's $1.30 just for connections, every refresh. With the candidate filter, we cap at 12 calls, around $0.04. The candidate filter does the bulk of the work for free.

2. **Quality.** Sending Claude all 30 items at once and asking "find the connections" produces vague, hedged output. Sending two items at a time, with a focused question, produces sharp judgments.

3. **Composability.** The candidate filter is independently swappable. We use TF-IDF; you could swap it for embeddings (Voyage, OpenAI text-embedding-3) without touching the judge. Embeddings would catch more semantic links that don't share keywords. We didn't use embeddings because they require either an extra API key or a downloadable model. TF-IDF works with no extra dependency.

### Why no embedding-based connections in v1

We seriously considered Voyage embeddings (Anthropic's recommended embedding provider). Two reasons we held off:

1. **Extra credential.** The system runs with one API key (Anthropic). Adding a second mandatory key violates the simplicity goal.
2. **Local embedding models** (sentence-transformers) require downloading a 100-MB-plus model file on first run, which adds a noticeable installation step.

In production, embeddings are absolutely the right answer. They'd catch links that TF-IDF misses, like "platform feels slower" on Reddit paired with "perf regression in commit X" on GitHub. We'd swap in Voyage 3 with a prefix budget of around 300 tokens per item. Cost would be roughly $0.001 per refresh, negligible.

### Why drop Low confidence

We started with showing all three confidence tiers in the UI. The result was that analysts learned to ignore the Low-confidence connections (because they were often wrong) and the High-confidence ones (because they were rare). The dashboard taught them to scan only Medium-confidence connections.

Cleaner is just to drop Low. The judge's calibration target is "if you're not sure, say not connected." Low confidence is the model's escape hatch when it's torn. Better to skip those entirely than to pollute the cards.

### Connection cost is bounded

By design. Whether a refresh has 10 items or 100 items, we judge at most 12 candidate pairs. This means connection cost is predictable. The trade-off is that with 100 items, we miss real connections in the long tail. The roadmap addresses this with embeddings, which would let us push the candidate gate higher because the candidate quality would be higher.

---

## 8. Concurrency, rate limits, and failure modes

### Within a refresh

* **Source ingestion.** All six sources are called concurrently via `asyncio.gather`. Reddit's per-subreddit calls are serialized (with a 600 ms gap) because Reddit's free tier doesn't tolerate burst access. The other five sources fire in parallel.
* **Per-item summarization.** All new items are processed concurrently via `asyncio.gather`, with a `Semaphore(4)` cap in `llm.py` to stay inside Anthropic's tier-1 rate limit (8000 output tokens per minute). The cap is a setting; raising it is a one-line change for higher-tier accounts.
* **Connection judging.** Up to 12 judge calls fire concurrently, also gated by the same semaphore.

### Across refreshes

* **Single ticker, repeated refresh.** Cross-refresh dedup makes this cheap. The orchestrator reads the previous snapshot and reuses summaries for unchanged items. Only new items hit the LLM.
* **Multiple tickers refreshed at once.** Each ticker is independent. Snapshot files don't share state. You could fan out to all 9 tickers in parallel from a scheduler. The Anthropic rate limit becomes the bottleneck before anything else.

### Anthropic API failures

`llm.py` wraps every Claude call with `tenacity` retries: 3 attempts, exponential backoff (2s, 4s, ... up to 20s), retry on any exception. The retry policy is uniform across all call sites (per-item summary, Brain, judge, chat) because every Claude call has the same recovery story.

If retry exhaustion happens for a per-item summary, the item gets a fallback summary that's just the title, marked as relevance 1. The dashboard still shows the item; the Brain just doesn't synthesize from it.

If retry exhaustion happens for the Brain Summary, the snapshot is saved with `brain=None` and the dashboard shows a "Brain summary unavailable" empty state with a suggestion to retry.

If a connection judge fails, that one connection is just dropped. The other 11 still surface.

### Source failures

Each source's `fetch` is wrapped in `_safe_fetch` in the orchestrator. Any uncaught exception is logged at WARN level and converted to `(items=[], status="failed: {exception}")`. The status string reaches the dashboard's source-status row. The user sees something like "failed: HTTP 503" in the GitHub column and knows to try again later. The other sources aren't affected.

The Reddit unauthenticated tier is the most fragile. We've seen it return 403 from datacenter IP ranges (which is why the dev sandbox had trouble). On a residential IP with a browser-class User-Agent, it works reliably enough to be the primary path. If it consistently fails for a particular deployment, the production move is to swap in PRAW with OAuth (a one-screen change in `reddit_source.py`).

### Rate-limit hits

* **GitHub.** Without a token, 60 requests per hour shared. Easy to hit. We log a `rate-limited` status and the analyst sees it. The recommendation is to set `GITHUB_TOKEN` (free, 5000 per hour).
* **X.** Rate-limited on the recent-search endpoint at 1500 requests per 15 minutes per app on Basic. We log `rate-limited` if 429 is returned.
* **Reddit.** Rate-limited at the source-IP level, no easy way to inspect the threshold. We pace at 600 ms per subreddit call to stay polite.
* **HN, EDGAR, Google News.** No documented rate limits within reasonable use.

---

## 9. Prompt engineering choices

### Three prompts, each with a single purpose

* **The summarizer prompt** in `llm.py`. Reads one item, returns a structured JSON note with summary, sentiment, relevance, and themes. Frames the model as a buy-side analyst. Has explicit anti-promotional rules and an explicit forbidden-phrase list for plain English.

* **The Brain prompt** in `brain.py`. Reads all the surviving items, returns the nine-section Brain Summary. Frames the model as the senior alt-data analyst. Includes the closed-world rule, the citation rule, the plain-English forbidden phrases, the question-count calibration, examples of good and bad management questions, and the structured JSON shape with field-by-field rules.

* **The connection judge prompt** in `connections.py`. Reads exactly two items, returns a Boolean and a confidence rating. Includes the explicit "topical adjacency is not a connection" instruction. Single-purpose, focused.

Why three prompts instead of one big agent? Because each task has a single output shape, and the validation of that shape is different. The summarizer's output has a strict relevance integer; the Brain's output has a citation requirement; the judge's output has a confidence enum. Forcing all three into one agent loop would obscure these contracts.

### Why temperature 0.3

Low enough that citations stay stable and structured fields (relevance, sentiment, confidence) don't bounce between runs. High enough that the prose reads naturally and not like templated boilerplate. We tested temperature 0.0 and 0.5. At 0.0 the prose felt mechanical; at 0.5, the citations occasionally drifted between repair runs. 0.3 is the right middle.

### Why JSON output instead of structured tool calls

Sonnet 4.6 supports tool use. We could have used `tool_use` with strict schema enforcement instead of asking for JSON in the response. We chose JSON for two reasons:

1. **Repair loop simplicity.** The repair loop reads the previous output, finds the validation errors, and asks the model to fix them. This is much cleaner with JSON-in-text than with tool-call schemas, because we can quote the previous text back to the model.
2. **Fewer moving pieces.** No tool registration, no tool dispatch, just `messages.create` and a JSON parse.

We also have a Pydantic model for every prompt's output, so the schema enforcement happens at parse time. The model just produces text.

### Why Pydantic for everything

Every value that crosses a module boundary has a defined Pydantic type. This catches type bugs at the boundary, not in production. The validator is a function that returns a list of human-readable error strings, which is exactly the format we want to feed back to the repair-loop prompt.

### Why no agentic tool use for the Brain

We considered an agent loop where the Brain has tools to "fetch more items" or "look up a specific Reddit post." We didn't build it because the items are pre-loaded into the prompt. The Brain has everything it needs at call time. Adding tool use would add latency (extra round trips) and add a vector for non-determinism without adding signal.

For the chat endpoint, similarly: the snapshot is pre-loaded into the chat prompt. The chat doesn't need to fetch anything mid-conversation.

---

## 10. Evaluation, both built and planned

### What's built

* **Citation enforcement and repair loop.** Described above. This catches the obvious hallucinations.
* **Confidence-graded connection judge with Low filtering.** This catches keyword-overlap false positives.
* **UI-side citation cross-check.** Belt-and-suspenders.
* **Smoke tests.** A simple `python -c` test sweep that imports the API, runs each source live, and confirms the output shape. Not a full eval harness, but enough to catch regressions during development.

### What's in the roadmap, with rationale

A real production eval harness would have three pieces:

1. **Snapshot golden file per ticker.** A human-graded set of "true connections this ticker should surface in the next 30 days" (positive examples) and "false connections this ticker should NOT surface" (negative examples). The system runs daily and compares its outputs against the golden file. Precision and recall on connections become a dashboard.

2. **Brain Summary panel review.** Each week, three analysts read 5 randomly-sampled Brain Summaries and rate them on 1-to-5 scales for Accuracy, Usefulness, and Forwarding-Worthiness. The averages get plotted over time. Prompt changes that drop these scores get rolled back.

3. **Cited-claim spot-check.** For each Brain Summary, randomly sample 3 cited claims. A reviewer reads the original source item and checks: does the source actually support the claim? This catches subtle semantic drift that the citation validator can't.

We didn't build (1), (2), or (3) because each requires a manual labelling effort that's outside the scope. The architecture supports them: connections are explicitly typed with confidence levels, snapshots are versioned by file, and the Brain Summary is structured JSON. Building the eval harness on top is a clean addition, not a refactor.

### How we'd compare against analyst reads

This is what the brief asks about. The simplest version: take a set of analyst-written notes from the firm's research database, pick the ones that turned out to be right (positive analyst calls that played out), and ask "what's in the analyst note that the Brain DIDN'T have?" Then "what's in the Brain that the analyst note didn't?" The first question tells us where to add sources; the second tells us where the Brain is genuinely additive.

A more rigorous version uses time-travel: refresh the Brain on a Friday afternoon for ticker X, save the snapshot. Look at the analyst's note from the following Monday. Compare. Did the Brain surface the same signal earlier? Or did the analyst find something the Brain missed?

This isn't built, but it's the right test, and it's what production validation should look like.

---

## 11. Cost and latency

### Cost per refresh

Numbers below are observed on tier-1 Anthropic rate limits. The dashboard's inline metrics strip and the dollar-sign modal show the actual numbers for whatever you just ran.

| Phase | Calls | Avg tokens (in / out) | Cost |
|---|---|---|---|
| Per-item summarization | ~25 | 600 / 90 each | ~$0.06 |
| Brain Summary, plus optional repair | 1 to 2 | 5000 / 800 | ~$0.03 |
| Connection judges | up to 12 | 400 / 80 each | ~$0.03 |
| **Cold refresh, total** | ~40 | | **~$0.12** |
| Warm refresh (with dedup) | ~10 | | **~$0.04** |
| Chat turn | 1 | 5000 / 250 | ~$0.02 |

Pricing at Sonnet 4.6 list rates: $3 per million input tokens, $15 per million output tokens.

### Latency

* **Cold refresh.** 60 to 90 seconds end to end. Reddit's polite per-subreddit pacing is the dominant wall-clock contribution (6 subreddits times 600 ms equals 3.6 seconds spent purely waiting). The summarization stage is roughly 10 to 15 seconds at concurrency 4. Brain is 10 to 30 seconds depending on input length. Connections is 5 to 15 seconds depending on candidate count.
* **Warm refresh.** 30 to 50 seconds, dominated by the source pulls themselves. The summarization stage drops dramatically because most items are reused.
* **Chat turn.** 2 to 4 seconds.

### Where the bottlenecks are, and how to relax them

* **Anthropic rate limit, tier 1.** 8000 output tokens per minute. The summarizer concurrency is set at 4 to stay inside this. On tier 2 (40K out/min), we'd raise it to 10 and refreshes would drop to 30 seconds.
* **Reddit pacing.** 600 ms per subreddit. We could parallelize across subreddits if Reddit tolerated it, but we hit 429 errors when we tried. Production fix is OAuth (60 requests per minute per token), which would let us hit subreddits in parallel.
* **GitHub at 60 per hour.** A single ticker refresh hits GitHub roughly 8 to 10 times. So on the unauthenticated tier, you can refresh maybe 5 to 6 tickers per hour before you 429. With a token (5000 per hour), this stops being a bottleneck.
* **TF-IDF candidate gen.** Scales O(n^2) on item count. At 30 items it's instant. At 300 items it would still be fine (90,000 pairs computed in milliseconds). At 3,000 items we'd want a smarter candidate retrieval, but we're nowhere near that.

### Cost at production scale

* 200 tickers, daily cold refresh: ~$24 per day, ~$720 per month.
* 200 tickers, daily warm refresh: ~$8 per day, ~$240 per month.
* Assuming hour-stale Reddit/HN/News refresh on 200 tickers: dominated by warm cost, around $20 per day.

These are LLM costs only. Compute and storage are negligible.

The biggest cost lever past warm dedup is mix-model: route per-item summarization to Haiku 4.5 (around 70 percent cheaper) and reserve Sonnet 4.6 for the Brain Summary and the connection judge. We didn't ship this because the case study set Sonnet 4.6 as the default model, but it's a clean addition. With mix-model, 200-ticker daily warm refresh drops to roughly $80 per month.

---

## 12. Per-ticker fan-out, and the path from 9 to 200 tickers

### What's there today

`refresh_ticker(ticker, time_window)` is the unit of work. It's a pure async function. It reads `data/tickers.json`, fetches from six sources, summarizes new items, runs the Brain pass, runs the connections pass, and writes one JSON file. Two calls to `refresh_ticker` for two different tickers don't share state and don't conflict.

### What changes at 200 tickers

Three things:

1. **Refresh runs from a scheduler, not the UI button.** EventBridge plus Lambda on AWS, or a Container Apps Job on a timer trigger on Azure. Both are essentially `cron` for the cloud.
2. **Snapshots move to object storage.** S3 or Blob. The `storage.py` module is the only file that changes; the function signatures are identical. The Pydantic schema stays the same.
3. **Cross-refresh dedup extends from per-id to content-hash.** Today, an item is "new" if its id wasn't in the previous snapshot. At 200 tickers we'd add a global content-hash table so that a Reddit post quoted in three places gets summarized once across all tickers. This drops cost another 10 to 20 percent.

### Cadence per source at 200 tickers

* **Reddit, Industry News, X.** Hourly. These have the freshest signal.
* **Hacker News.** Every 4 hours. Lower volume of new content.
* **GitHub.** Every 6 hours. Issues and releases don't change minute-to-minute.
* **EDGAR.** Daily. Filings are slow.

The orchestrator function signature accepts a `time_window` argument, which the cron job sets per-source. The same function is called either way.

### Where the 200-ticker scale would actually break

* **Reddit unauth limit.** At 200 tickers refreshing hourly, that's around 1200 Reddit subreddit calls per hour. The unauthenticated tier won't tolerate this. We'd swap to OAuth.
* **Anthropic API tier.** At 200 tickers warm-refreshed daily, we'd be making roughly 2000 Sonnet 4.6 calls per day. Tier 1 is fine. Tier 2 becomes more comfortable.
* **DynamoDB per-ticker write hot spot.** If we move to DynamoDB and 200 refreshes fire at the same minute, we might see throttling. The fix is to spread refresh start times across the cron window.

None of these are architectural problems. They're operational ones, all addressable with cloud-side configuration changes.

### Adding a source without a redeploy

The brief asks about this. Today, adding a source requires:

1. Drop a new `sources/foo_source.py` file.
2. Register one line in `orchestrator.refresh_ticker`.
3. Redeploy.

This is a real redeploy. To make it config-driven, the path is:

1. Move the orchestrator's source list into `data/sources.yaml` (or an equivalent config file).
2. Add a plugin loader that imports source modules dynamically based on the config.
3. Optionally, allow new sources to be uploaded as packaged Python wheels and loaded at startup.

This is a roadmap item. We didn't build it because the case study has 6 sources and the lowest-friction way to ship was the explicit imports. In production with 12-plus sources, the config-driven plugin pattern is the right shape.

---

## 13. Risks

### Hallucinated connections

Already covered in detail. Mitigated by the four-layer defense, but not zero. Production hardening adds the eval harness.

### Source bias

Reddit skews young, male, retail. GitHub skews developer, not buyer. X skews short-time-horizon. Industry News skews press-release-amplified. The help modal flags this so the analyst reads each column with the right lens. The next iteration would attach a per-item audience tag (`audience: developer-operator`, `audience: retail-trader`) so a sentiment chip can't be misread as buyer sentiment.

EDGAR is the partial counter-balance: it's the authoritative anchor. But EDGAR is also lagged, structured, and incomplete (it doesn't tell you that customers are unhappy until customers cancel, which shows up two quarters later in the 10-Q). The combination of all six sources is more balanced than any one alone.

### Closed-world chat is conservative

The chat will refuse questions the items can't support. A user asking "how does this compare to NVIDIA?" gets a "the items don't show that" if NVIDIA isn't in the items. This is by design, to prevent hallucination. It also means the chat is less useful for hypothetical or comparative questions than a free-form Claude session would be.

The roadmap has a "knowledge mode" toggle that opens the door to general knowledge with answers explicitly flagged as such. The flagging matters: the user has to know which mode they're in.

### Data licensing

All current sources are public APIs (with X gated behind a paid tier the integration plugs into seamlessly). Production additions (Glassdoor, App Store, podcasts) require licensed feeds. Vendor cost at production scope is in the high four-figures monthly. Real money, but the brief explicitly calls out these as the sources where alpha lives, and the alpha justifies the spend for a TMT shop.

### Scaling risks

Mostly operational, covered in Section 12. The architecture doesn't break. The cloud-side configuration evolves.

### Prompt drift

The system uses three prompts. Changes to any of them ripple through the output. We don't have a regression test for prompt changes today. The roadmap eval harness fixes this.

### Vendor risk on Anthropic

The system is hard-bound to Sonnet 4.6 today. Switching to a different LLM provider would require rewriting the Anthropic-specific tool-use and message formatting in `llm.py`. This is contained (one file) but real. The mitigation is to run the prompts through a thin abstraction layer; we didn't build this because YAGNI, but it's a one-day refactor when needed.

---

## 14. Roadmap

In rough priority order, with rationale:

1. **Licensed Glassdoor / Levels.fyi feed** via Revelio Labs or Thinknum. Sales reorganizations and layoffs are the highest-value missing source for TMT specifically. Cost is real but the alpha justifies it for the firm's mandate.
2. **Embedding-based connection candidates** using Voyage 3 or text-embedding-3. Catches semantic links TF-IDF misses ("platform feels slower" plus "perf regression in commit X"). One-day implementation, $0.001 per refresh.
3. **Per-source incremental cursors.** `since_id` per source so steady-state refresh cost is per-new-item, not per-item. Drops cost another 10 to 20 percent past current dedup.
4. **Eval harness.** Snapshot-level golden tests, weekly analyst panel review, cited-claim spot-check. Makes prompt changes safe to ship.
5. **Mix-model cost mode.** Haiku 4.5 for ingestion summarization, Sonnet 4.6 reserved for Brain and judges. Around 70 percent cost reduction at the per-item layer.
6. **Slack and Teams export.** "Send this Brain Summary to #research-tmt" button. Analysts adopt tools faster when they ship to where work happens.
7. **Cross-ticker connections.** A Reddit thread on KVYO's Shopify dependency could link to a news article on Shopify's quarterly results. Same engine, one-line orchestration change.
8. **Plugin-loaded sources from config.** Adding a source without a redeploy.
9. **Knowledge mode toggle for chat.** Opens the closed-world to general knowledge, with explicit flagging.
10. **Per-item audience tag.** Surfaces "this is a developer voice, not a CFO voice" so sentiment chips can't be misread.

---

## 15. Build vs. buy

AlphaSense, Sentieo, and Tegus sit in adjacent space. AlphaSense in particular does sell-side and corporate document search far better than this dashboard does. They have the corpus, the licensing, the entity resolution, and the years of indexing investment. We can't compete with that.

What this Brain does that they don't: cross-source synthesis between true alt-data sources (Reddit, HN, X, GitHub) and authoritative filings, with explicit citation enforcement, confidence-graded connections, and a Position Read plus Management Questions output that maps directly onto a PM's decision process.

The right framing is that AlphaSense is the index and search layer for known documents. This Brain is the synthesis layer for noisy public alt-data. They're complementary, not competitive. A production deployment would put an AlphaSense link in the "see the underlying filing" affordance and use the Brain for the cross-source thesis the analyst forwards to a PM.

If the build-vs-buy question is "should we just buy AlphaSense and stop here," the answer is no. AlphaSense doesn't index Reddit, doesn't index Hacker News, doesn't pull GitHub issues, and doesn't synthesize across heterogeneous unstructured sources. It indexes documents in a corpus. That's a different product. You want both.

If the question is "should we build the document-search layer ourselves," the answer is also no. Buying that capability from AlphaSense is much faster and the result is much better than what we'd build in a year of in-house work.

The right portfolio is: license AlphaSense for document search, license Glassdoor and Levels via Revelio for employee-side signal, license app-store data via Sensor Tower, build the Brain for the cross-source synthesis. The Brain is the layer where the firm's edge actually lives. Everything else is commodity infrastructure.
