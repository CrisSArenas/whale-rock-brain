# Whale Rock Brain

A research dashboard for getting up to speed on a stock in 15 minutes, using sources Wall Street isn't reading well.

You pick a ticker. The system pulls fresh material from six places (Reddit, industry news, GitHub, Hacker News, X, and SEC filings), reads each piece, then writes a short, opinionated summary about what's actually going on. Every claim links back to the source. There's a chat box you can ask follow-up questions in. And there's a print button that produces a clean PDF you can forward to a PM.

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)
![Pydantic](https://img.shields.io/badge/Pydantic-2.6+-E92063.svg)
![Anthropic](https://img.shields.io/badge/Claude-Sonnet%204.6-D97757.svg)

---

## Contents

1. [What this is, in one paragraph](#what-this-is-in-one-paragraph)
2. [Why this exists](#why-this-exists)
3. [Run it on your laptop](#run-it-on-your-laptop)
4. [The tickers I included](#the-tickers-i-included)
5. [How to read the dashboard](#how-to-read-the-dashboard)
6. [The sources, plainly](#the-sources-plainly)
7. [Print or save as PDF](#print-or-save-as-pdf)
8. [What it costs to run](#what-it-costs-to-run)
9. [What about Twitter](#what-about-twitter)
10. [What it will not do, on purpose](#what-it-will-not-do-on-purpose)
11. [Adding a new ticker](#adding-a-new-ticker)
12. [Configuration](#configuration)
13. [Project layout](#project-layout)
14. [Deploying it for real](#deploying-it-for-real)
15. [Common questions](#common-questions)

---

## What this is, in one paragraph

Sell side notes are a commodity. Every fund reads the same things. The edge is in what's outside that loop: the Reddit thread where users are quietly leaving a product, the GitHub issue where a customer flags a real problem, the trade press article that names a customer the company hasn't disclosed. This dashboard pulls those signals together for one ticker at a time, has Claude read them, then writes a short note that says what the data is actually saying. It cites every claim. It tells you when the data is thin. It gives you the next questions to ask management.

---

## Why this exists

If you're an analyst at a long/short shop, the question you ask every day is "what does the Street not see yet?" That signal exists, but it's spread across dozens of places, most of which are noise. Reading them all costs hours. Reading the right ones, and noticing where two of them point at the same thing, costs even more.

The Brain does that reading for you. You give it a ticker. It pulls the freshest material from six different places, has Claude read every item, then writes a synthesis that connects the dots across sources. You read it for five minutes, decide if you trust it, and either go deeper or move on. The point is to compress the "first pass" of alt-data research from a half-day to a coffee.

---

## Run it on your laptop

You need Python 3.11 or newer and an Anthropic API key.

```bash
# Make a clean Python environment
python -m venv venv
source venv/bin/activate           # macOS / Linux
venv\Scripts\Activate.ps1          # Windows PowerShell

# Install the libraries
pip install -r requirements.txt

# Set up your API key
cp .env.example .env
# Open .env in any text editor and paste your Anthropic key on the right line
```

Then start the app:

```bash
python run_api.py
```

Open `http://localhost:8000` in your browser. Pick a ticker. Click **Refresh data**. The first refresh takes about a minute and a half because it's pulling live data and asking Claude to read every piece. After that, refreshes for the same ticker are faster because the system remembers what it already read.

That's it. No cloud setup, no database to install, no other keys to chase down. The five non-paid sources work right out of the box.

---

## The tickers I included

The dropdown at the top of the page is split into two groups:

**Case Study Tickers**, the names Whale Rock's analysts are working on:

* **AMD**, Advanced Micro Devices
* **SNDK**, Sandisk
* **FROG**, JFrog
* **APP**, AppLovin
* **KVYO**, Klaviyo

**Other Examples**, larger names included so you can see the system working on companies you already know:

* **MDB**, MongoDB
* **NVDA**, NVIDIA
* **META**, Meta Platforms
* **MSFT**, Microsoft

If you want to add a new ticker (your own portfolio name, or one your team is researching), see [Adding a new ticker](#adding-a-new-ticker) at the bottom. It's a one-file change.

---

## How to read the dashboard

The page has five things on it after you refresh. Read them in this order:

### 1. The metrics strip

A row of four cards just under the controls: how long the refresh took, how much it cost in LLM tokens, and how many input and output tokens went through. This is so you always know the unit economics of what you're looking at.

### 2. The Brain Summary card

This is the answer. It's the navy block that takes up most of the screen.

It has these sections, in this order:

1. **Headline.** One sentence. The thing you'd forward to a PM.
2. **Position read.** A gold-tinted block under the headline. Two or three sentences saying what this data argues for or against. Not a buy/sell rating, just analyst commentary. Includes a Confidence pill (Low, Medium, or High) with a one-line rationale.
3. **Narrative.** Two or three short paragraphs synthesizing across all the sources. Every claim links back to the source it came from.
4. **What's changing.** Three to five bullets on concrete shifts since the last data point.
5. **What to watch.** Two to four specific things that, if they happen, would confirm or invalidate the read. These are the "next data points" you should be looking for.
6. **Non-consensus bear and bull.** The two cases the sell side isn't writing. Cited.
7. **Questions for the next management call.** A numbered list of one to five specific questions to ask on the next earnings call. The number scales with how much real signal there is. If the data is thin, you get one or two. If it's rich, you get four or five.

The little chips like `[R-7f3a]` are clickable. Click one and the page scrolls to the source item the claim came from, with a brief highlight.

### 3. The source feeds

Below the Brain Summary, six columns showing every item the system pulled. They're in this order: **Reddit, Industry News, GitHub, Hacker News, X, SEC EDGAR.**

Each item shows a one line summary, a sentiment chip (bullish, bearish, neutral, mixed), a relevance score from 1 to 10, and the item's ID (the same ID the Brain Summary cites). Click any item to open the original Reddit post, news article, GitHub issue, tweet, or SEC filing in a new tab.

The status row at the top of each column tells you how that source did on the last refresh. "ok: 18 items" means it returned 18 items. "rate-limited" means the source is busy. "skipped: X_BEARER_TOKEN not set" means you haven't enabled X yet (it's the only paid one).

If a column is thin (fewer than three items), there's a button under it that says "Try 3 months window" or similar. Click it and the system re-runs the search across a wider time window, just for that ticker, and shows you what it finds.

### 4. The Connections cards

Below the source feeds, a list of cross-source links the system found. For example: a Reddit thread complaining about service quality plus a GitHub issue about a specific bug plus a news article about a sales reorg, all pointing at the same theme.

Each card shows the theme, a short rationale from Claude on why these items are connected, the two source IDs (clickable), and a confidence rating (Medium or High, the system filters out Low). Below that, the raw similarity score so you can see the math.

### 5. The chat box, bottom right

Slides up when you click it. Ask any question about what's on the screen. The system answers using only the items you can see, with citations. If the answer isn't in the data, it tells you so plainly. It will not invent things.

There are also three small buttons in the top-right corner:

* The **printer** opens a clean Brain Summary report in a new window, ready to print or save as PDF.
* The **dollar sign** opens a window showing every individual LLM call from the last refresh, with per-call cost.
* The **question mark** opens a step-by-step guide to using the dashboard.

---

## The sources, plainly

Six sources. Each one captures something the others don't.

**Reddit.** Where retail investors and product users argue and complain. Subreddit-aware, so for AMD it pulls from r/AMD_Stock and r/buildapc, for FROG from r/devops, for KVYO from r/shopify. The "operator complaint that lines up with a guidance miss" signal lives here. No setup needed.

**Industry News.** Trade publication coverage via Google News' free RSS. Picks up niche outlets and channel-specific blogs that Bloomberg buckets together. The articles you'd never find unless you knew where to look. No setup needed.

**GitHub.** Recent issues and software releases for the company's repos, plus a wider search for product mentions in third-party code. This is the operational signal that's hardest to fake: how often the company ships, what customers complain about publicly, who's quietly building integrations. No setup needed for basic use; an optional GitHub token raises the rate limit a lot if you want to refresh many tickers in a row.

**Hacker News.** Where developer narratives crystallize months before sell side picks them up. Founder commentary, infrastructure post-mortems, "we just migrated off X" threads. Especially valuable for FROG, KVYO, APP, and MDB, where the buyer is a developer. No setup needed.

**X (Twitter).** Real time financial Twitter, breaking news commentary, follower-weighted signal. This one needs a paid API key (see [What about Twitter](#what-about-twitter)). The system aggressively filters pump posts and FOMO content so what you see is real commentary, not "AMD to the moon" noise.

**SEC EDGAR.** The grounding source. Latest 8-K, 10-Q, 10-K, Form 4, 13F filings. This is the truth set: every other source is opinion, EDGAR is what the company actually told the SEC. EDGAR always returns the most recent filings regardless of what time window you pick.

**What I left out, and why.** Glassdoor and Levels.fyi would be excellent for catching layoffs and sales reorganizations early, but they don't have a free API. To use them in production I'd license a vendor feed (Revelio Labs, Thinknum). App Store and Play Store reviews need licensed wrappers (Sensor Tower, data.ai). Podcast transcripts would need a Whisper transcription pipeline running separately. All three are listed as next steps. Stack Overflow I tried and dropped: developer Q&A volume has fallen as people moved to AI assistants and Discord, and the signal that's left mostly duplicates GitHub.

---

## Print or save as PDF

Click the printer icon in the top right. A new tab opens with a clean Brain Summary report. From there, hit Print and pick "Save as PDF" as your destination, or print it directly.

The exported document has a cover header with the ticker, when it was generated, the time window, the item count, and the LLM cost. Then the full Brain Summary: headline, Position Read with confidence pill, narrative, what's changing, what to watch, bear case, bull case, and the numbered Questions for Management.

The source feeds and connection cards are deliberately left out of the print. The print is the synthesized read, not the raw data. Forward it to a PM. File it in your deal book. Email it to yourself.

---

## What it costs to run

The dashboard shows you the actual cost of every refresh, but for planning purposes:

| What | When | Cost |
|---|---|---|
| One refresh, first time | Cold start | Around 12 cents |
| One refresh, after a previous refresh | Warm (uses cached summaries) | Around 4 cents |
| One chat question | | Around 2 cents |
| Refreshing all nine tickers | Cold | About a dollar |
| Refreshing all nine tickers daily, 30 days | Warm | About 12 dollars/month |
| Running 200 tickers daily, warm | Production scale | Around 8 dollars/day |

Pricing is at Claude Sonnet 4.6 list rates ($3 per million input tokens, $15 per million output tokens). Cache discounts and rate-limit tier benefits are not modeled. The dollar-sign button in the top right shows you the exact breakdown for the last refresh.

---

## What about Twitter

X is the only source that needs a paid key. The free X tier doesn't allow the kind of search this needs. The dashboard works fine without it; the X column just shows "skipped: X_BEARER_TOKEN not set" and the other five sources do the work.

If you want X to work:

1. Go to `developer.x.com/en/portal/dashboard`, sign in.
2. Subscribe. Basic is $200 a month and gives you 10,000 tweets a month, which is enough for about 10 tickers refreshed every hour. Pro is $5,000 a month and gives you full archive search.
3. In the developer portal, create a Project and an App.
4. Click into the App, go to "Keys and tokens", generate a Bearer Token.
5. Paste it into your `.env` file as `X_BEARER_TOKEN`.
6. Restart `python run_api.py`.

The X column will populate on the next refresh. The system will pull up to 50 recent tweets, filter out pump/FOMO/spam content automatically, and show you what's left.

---

## What it will not do, on purpose

A few deliberate choices worth knowing about:

**The chat will not answer from outside knowledge.** If you ask "how does this compare to NVIDIA?" and the items don't mention NVIDIA, the chat will say "the items don't show that." This is on purpose. The cost of a confident wrong answer is much higher than the cost of an honest "I don't know."

**It will not give you a buy/sell rating.** The Position Read tells you what the data argues for or against, but the actual call is yours. The system can prepare your thinking, it can't replace it.

**It will not connect items just because they share words.** The connections engine has two passes: a fast math pass that finds candidates, then a Claude pass that decides if the candidates are real connections or just keyword overlap. Anything Claude flags as "topical adjacency, not a real connection" gets dropped before you see it.

**It will not fabricate a source ID.** The frontend cross-checks every citation chip against the actual list of items. A made-up ID would render as plain text, not a clickable chip.

---

## Adding a new ticker

Open `data/tickers.json`. Copy any existing entry. Edit:

* The ticker symbol at the top
* The company name
* The aliases (other names the company goes by)
* The relevant subreddits (anywhere people discuss the product or the stock)
* The GitHub orgs and keywords (if it's a tech company)
* A one-paragraph context note about what the company does and what the key debate is
* The `group` field, either `"case_study"` or `"examples"`, which controls which dropdown bucket it shows up in

Save the file. Restart the app. The new ticker is in the dropdown. No code changes needed.

---

## Configuration

Everything is in a `.env` file in the project folder. Copy `.env.example` to `.env` to start.

| Setting | Required | Default | What it does |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | yes | none | Your Claude API key. Get one at console.anthropic.com |
| `X_BEARER_TOKEN` | no | none | Enables the X source. Paid X tier required. |
| `GITHUB_TOKEN` | no | none | Raises GitHub rate limit from 60 per hour to 5000 per hour. Free at github.com/settings/tokens, no scopes needed. |
| `EDGAR_CONTACT_EMAIL` | no | research@example.com | The SEC asks for a real contact email in the request header. Replace with yours if deploying. |
| `MODEL` | no | claude-sonnet-4-6 | Which Claude model to use everywhere. |
| `MAX_TOKENS` | no | 2000 | Cap on how long any one Claude response can be. |
| `TEMPERATURE` | no | 0.3 | How random the Claude output is. Low so citations stay stable, not zero so the prose still reads naturally. |

---

## Project layout

```
Case_Study_Folder/
  src/whale_rock_brain/
    api.py             # The web app endpoints
    brain.py           # The Brain Summary writer
    chat.py            # The chat answerer
    config.py          # Reads the .env file
    connections.py     # Finds and validates cross-source connections
    llm.py             # Talks to Claude, tracks cost
    observability.py   # Logging, cost tracking
    orchestrator.py    # Runs the full refresh pipeline
    schemas.py         # Pydantic data models
    storage.py         # Reads and writes the snapshot JSON files
    sources/
      reddit_source.py
      news_source.py
      github_source.py
      hn_source.py
      x_source.py
      edgar_source.py
  frontend/
    index.html         # The single-page dashboard
  data/
    tickers.json       # The list of tickers and their config
    snapshots/         # Where refreshed data is saved, one file per ticker
  run_api.py           # Starts the app
  Dockerfile           # For running it in a container
  requirements.txt     # Python dependencies
  .env.example         # Template for your config
  README.md            # This file
  DESIGN.md            # The technical design doc
```

---

## Deploying it for real

A Docker file is included. To run the dashboard in a container:

```bash
docker build -t whale-rock-brain .
docker run --rm -p 8000:8000 --env-file .env whale-rock-brain
```

For a real production deployment, the design doc has paths for AWS and Azure. The short version:

**On AWS:** put the container in App Runner or ECS Fargate, run scheduled refreshes from EventBridge plus Lambda or Fargate tasks, store snapshot files in S3, keep secrets in Secrets Manager, send logs to CloudWatch. Total monthly cost at production scale (200 tickers, daily refresh, warm cache) is in the low hundreds.

**On Azure:** Container Apps for the dashboard, a separate Container Apps Job on a timer trigger for the scheduled refresh, Blob Storage for snapshots, Key Vault for secrets, Application Insights for logs.

In either case, the only file you'd change in the codebase is `storage.py`, swapping local file reads/writes for the cloud storage client. The rest of the pipeline doesn't care where the snapshots live.

---

## Common questions

**Can I trust what the Brain says?** Trust but verify. Every claim has a citation. Click the chip, read the original source, decide for yourself. The system has four separate guardrails against making things up (closed-world prompts, a citation validator with a repair pass, a confidence-graded connection judge, and a frontend cross-check), but the final read is yours.

**What happens if Reddit is down?** The other five sources keep working. The Reddit column shows a "failed" or "rate-limited" status at the top. The Brain Summary uses what it has. Your dashboard never breaks because of one source.

**How fresh is the data?** Whatever the time window you pick. Default is "last month." You can pick 1 week, 3 months, 6 months, or 1 year. EDGAR ignores the window and always shows you the most recent filings, because those are the authoritative anchor.

**What if I run a second refresh right after the first?** The system remembers items it already read and skips them. Only net-new items get a fresh Claude pass. So a same-day re-refresh costs maybe 4 cents instead of 12.

**Why is X the only paid source?** Because X (Twitter) is the only one whose owner has decided to charge for API access. Reddit, Hacker News, Google News, GitHub, and SEC EDGAR all expose enough through public endpoints to do real work for free.

**Can I run this against a private list of tickers?** Yes. Add them to `data/tickers.json`. Nothing in the dashboard or the pipeline cares which tickers are in the file.

**What if the Brain finds nothing interesting?** It tells you. The headline will say something like "Limited material, mostly press releases and tangential mentions." The confidence pill will be Low. The system is built to admit when the data is thin, not to pad a summary to feel productive.
