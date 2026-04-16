"""
Claude Viral Tweet Tracker — Backend API
Fetches top viral tweets about Claude (Anthropic) every 24 hours,
ranks by engagement, stores history, and serves via REST API.

Uses Apify apidojo/tweet-scraper actor for tweet search ($0.40/1K tweets).
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import re
import anthropic
import httpx
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from contextlib import asynccontextmanager
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APIFY_API_TOKEN = os.environ.get("APIFY_API_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
APIFY_ACTOR_ID = "apidojo~tweet-scraper"
APIFY_BASE = "https://api.apify.com/v2"

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
CRON_HOUR = int(os.environ.get("CRON_HOUR", "9"))  # UTC hour for daily run
CRON_MINUTE = int(os.environ.get("CRON_MINUTE", "0"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Search terms — uses Twitter advanced search syntax inside Apify searchTerms
# Each entry becomes a separate searchTerms item for the actor
SEARCH_QUERIES = [
    '"Claude AI"',
    '"Claude Anthropic"',
    "@AnthropicAI Claude",
    '"Claude Opus"',
    '"Claude Sonnet"',
    '"Claude Haiku"',
]

# Maximum tweets to request from Apify per run
MAX_TWEETS_PER_RUN = 500

# Engagement score weights
WEIGHTS = {
    "impressions": 0.3,
    "retweets": 10,
    "likes": 3,
    "replies": 5,
    "quotes": 8,
    "bookmarks": 6,
}

# Minimum followers to filter out bots/spam
MIN_FOLLOWERS = 100

# Words that indicate the tweet is about a person named Claude, not the AI
EXCLUDE_PATTERNS = [
    "claude giroux",
    "claude kelley",
    "claude johnson",
    "claude rains",
    "claude debussy",
    "claude monet",
    "claude dallas",
    "claude speed",
    "gta",  # GTA character named Claude
    "grand theft auto",
]

# Maximum time (seconds) to wait for the Apify actor run to finish
APIFY_RUN_TIMEOUT = 300  # 5 minutes
APIFY_POLL_INTERVAL = 5  # seconds between status checks

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Storage (JSON files — cheap, simple, no DB needed)
# ---------------------------------------------------------------------------

DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_data_path(date_str: str) -> Path:
    """Return path for a given date's data file: data/2026-03-01.json"""
    return DATA_DIR / f"{date_str}.json"


def save_daily_data(date_str: str, data: dict):
    """Save a day's results to disk."""
    path = get_data_path(date_str)
    path.write_text(json.dumps(data, indent=2, default=str))
    logger.info(f"Saved data for {date_str} → {path}")


def load_daily_data(date_str: str) -> Optional[dict]:
    """Load a day's results from disk."""
    path = get_data_path(date_str)
    if path.exists():
        return json.loads(path.read_text())
    return None


def list_available_dates() -> list[str]:
    """List all dates that have data, sorted descending."""
    dates = []
    for f in DATA_DIR.glob("*.json"):
        if f.stem.count("-") == 2:  # YYYY-MM-DD format
            dates.append(f.stem)
    return sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# Apify Tweet Scraper Client
# ---------------------------------------------------------------------------


async def run_apify_tweet_scraper(search_terms: list[str], max_items: int = MAX_TWEETS_PER_RUN) -> list[dict]:
    """
    Run the apidojo/tweet-scraper Apify actor and return the results.

    1. Start an actor run via POST /v2/acts/{actor_id}/runs
    2. Poll until the run status is SUCCEEDED (or timeout)
    3. Fetch dataset items from /v2/datasets/{dataset_id}/items
    """
    if not APIFY_API_TOKEN:
        logger.warning("No APIFY_API_TOKEN set — skipping tweet collection")
        return []

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {APIFY_API_TOKEN}",
    }

    # Build the actor input — one run with all search terms
    actor_input = {
        "searchTerms": search_terms,
        "sort": "Top",
        "maxItems": max_items,
        "tweetLanguage": "en",
    }

    logger.info(f"Starting Apify actor run with {len(search_terms)} search terms, maxItems={max_items}")

    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1: Start the actor run
        try:
            start_resp = await client.post(
                f"{APIFY_BASE}/acts/{APIFY_ACTOR_ID}/runs",
                headers=headers,
                json=actor_input,
            )
            start_resp.raise_for_status()
            run_data = start_resp.json().get("data", {})
            run_id = run_data.get("id")
            dataset_id = run_data.get("defaultDatasetId")

            if not run_id:
                logger.error(f"Apify run did not return a run ID. Response: {start_resp.text[:500]}")
                return []

            logger.info(f"Apify actor run started: run_id={run_id}, dataset_id={dataset_id}")

        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to start Apify actor: {e.response.status_code} — {e.response.text[:500]}")
            return []
        except Exception as e:
            logger.error(f"Error starting Apify actor: {e}")
            return []

        # Step 2: Poll for run completion
        elapsed = 0
        status = "RUNNING"
        while elapsed < APIFY_RUN_TIMEOUT and status in ("RUNNING", "READY"):
            await asyncio.sleep(APIFY_POLL_INTERVAL)
            elapsed += APIFY_POLL_INTERVAL

            try:
                poll_resp = await client.get(
                    f"{APIFY_BASE}/actor-runs/{run_id}",
                    headers={"Authorization": f"Bearer {APIFY_API_TOKEN}"},
                )
                poll_resp.raise_for_status()
                status = poll_resp.json().get("data", {}).get("status", "UNKNOWN")
                logger.info(f"  Run status: {status} (elapsed: {elapsed}s)")

            except Exception as e:
                logger.warning(f"  Error polling run status: {e}")
                continue

        if status != "SUCCEEDED":
            logger.error(f"Apify actor run did not succeed. Final status: {status}")
            return []

        # Step 3: Fetch dataset items
        if not dataset_id:
            logger.error("No dataset ID available — cannot fetch results")
            return []

        try:
            items_resp = await client.get(
                f"{APIFY_BASE}/datasets/{dataset_id}/items",
                params={"format": "json", "limit": max_items},
                headers={"Authorization": f"Bearer {APIFY_API_TOKEN}"},
            )
            items_resp.raise_for_status()
            items = items_resp.json()

            if not isinstance(items, list):
                logger.error(f"Expected list from dataset items, got: {type(items)}")
                return []

            logger.info(f"Fetched {len(items)} tweets from Apify dataset")
            return items

        except Exception as e:
            logger.error(f"Error fetching dataset items: {e}")
            return []


# ---------------------------------------------------------------------------
# Tweet Processing & Ranking
# ---------------------------------------------------------------------------


def compute_engagement_score(tweet: dict) -> float:
    """Compute weighted engagement score from tweet metrics."""
    likes = int(tweet.get("likeCount", 0) or 0)
    retweets = int(tweet.get("retweetCount", 0) or 0)
    replies = int(tweet.get("replyCount", 0) or 0)
    quotes = int(tweet.get("quoteCount", 0) or 0)
    bookmarks = int(tweet.get("bookmarkCount", 0) or 0)
    impressions = int(tweet.get("viewCount", 0) or 0)

    score = (
        impressions * WEIGHTS["impressions"]
        + retweets * WEIGHTS["retweets"]
        + likes * WEIGHTS["likes"]
        + replies * WEIGHTS["replies"]
        + quotes * WEIGHTS["quotes"]
        + bookmarks * WEIGHTS["bookmarks"]
    )
    return round(score, 0)


def is_about_claude_ai(tweet_text: str) -> bool:
    """Filter: ensure the tweet is about Claude AI, not a person named Claude."""
    text_lower = tweet_text.lower()

    # Must contain at least one AI-related signal
    ai_signals = [
        "ai", "anthropic", "llm", "model", "opus", "sonnet", "haiku",
        "token", "context", "prompt", "api", "code", "coding", "chatbot",
        "assistant", "benchmark", "hallucin", "neural", "language model",
        "machine learning", "ml", "deep learning", "transformer",
        "gpt", "openai", "gemini", "computer use", "artifacts",
        "reasoning", "chain of thought", "agi", "artificial intelligence",
    ]

    # Check exclusions first
    for pattern in EXCLUDE_PATTERNS:
        if pattern in text_lower:
            return False

    # Check for AI signals
    for signal in ai_signals:
        if signal in text_lower:
            return True

    # If the tweet mentions @AnthropicAI, it's almost certainly about the AI
    if "@anthropicai" in text_lower:
        return True

    return False


def normalize_tweet(raw: dict, skip_claude_filter: bool = False) -> Optional[dict]:
    """Convert Apify tweet-scraper output format to our standard format.
    
    Args:
        raw: Raw tweet dict from Apify
        skip_claude_filter: If True, skip the is_about_claude_ai filter
                            (used for custom keyword/username searches)
    """
    try:
        # Skip non-tweet items (actor can return user profiles etc.)
        item_type = raw.get("type", "")
        if item_type and item_type != "tweet":
            return None

        text = raw.get("text", "")
        if not text:
            return None

        author = raw.get("author", {})
        author_handle = author.get("userName", "unknown")
        author_name = author.get("name", author_handle)
        author_followers = int(author.get("followers", 0) or 0)

        # Filter out low-follower accounts (spam/bot filter)
        if author_followers < MIN_FOLLOWERS:
            return None

        # Filter to AI context (only for Claude-specific daily collection)
        if not skip_claude_filter and not is_about_claude_ai(text):
            return None

        tweet_id = raw.get("id", "")
        likes = int(raw.get("likeCount", 0) or 0)
        retweets = int(raw.get("retweetCount", 0) or 0)
        replies = int(raw.get("replyCount", 0) or 0)
        quotes = int(raw.get("quoteCount", 0) or 0)
        bookmarks = int(raw.get("bookmarkCount", 0) or 0)
        # viewCount may not always be present in Apify output
        impressions = int(raw.get("viewCount", 0) or 0)
        created_at = raw.get("createdAt", "")

        # Build tweet URL — Apify provides url directly
        url = raw.get("url", f"https://x.com/{author_handle}/status/{tweet_id}")

        engagement_score = compute_engagement_score(raw)

        return {
            "id": str(tweet_id),
            "text": text,
            "author_handle": author_handle,
            "author_name": author_name,
            "author_followers": author_followers,
            "likes": likes,
            "retweets": retweets,
            "replies": replies,
            "quotes": quotes,
            "bookmarks": bookmarks,
            "impressions": impressions,
            "engagement_score": engagement_score,
            "url": url,
            "created_at": created_at,
        }
    except Exception as e:
        logger.error(f"Error normalizing tweet: {e}")
        return None


def deduplicate_tweets(tweets: list[dict]) -> list[dict]:
    """Remove duplicate tweets by ID."""
    seen = set()
    unique = []
    for t in tweets:
        if t["id"] not in seen:
            seen.add(t["id"])
            unique.append(t)
    return unique


# ---------------------------------------------------------------------------
# Timeframe Filtering
# ---------------------------------------------------------------------------

TIMEFRAME_MAP = {
    "4h": timedelta(hours=4),
    "12h": timedelta(hours=12),
    "24h": timedelta(hours=24),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
}


def parse_tweet_datetime(created_at: str) -> Optional[datetime]:
    """Parse a tweet's created_at timestamp into a timezone-aware datetime."""
    if not created_at:
        return None
    for fmt in [
        "%a %b %d %H:%M:%S %z %Y",  # Twitter/Apify format
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
    ]:
        try:
            dt = datetime.strptime(created_at, fmt)
            # Ensure timezone-aware (UTC)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def filter_by_timeframe(tweets: list[dict], timeframe: str) -> list[dict]:
    """Filter tweets to only include those within the given timeframe window."""
    delta = TIMEFRAME_MAP.get(timeframe)
    if not delta:
        return tweets  # Unknown timeframe, return all

    cutoff = datetime.now(timezone.utc) - delta
    filtered = []
    for t in tweets:
        dt = parse_tweet_datetime(t.get("created_at", ""))
        if dt and dt >= cutoff:
            filtered.append(t)
        elif not dt:
            # If we can't parse the date, include it (don't silently drop)
            filtered.append(t)
    return filtered


def keyword_matches(text: str, keyword: str) -> bool:
    """Check if keyword appears in tweet text (case-insensitive).
    Matches the keyword in text content and hashtags."""
    text_lower = text.lower()
    keyword_lower = keyword.lower()
    return keyword_lower in text_lower


# ---------------------------------------------------------------------------
# Daily Collection Job
# ---------------------------------------------------------------------------


async def run_daily_collection():
    """Main job: run Apify actor, rank results, save top 5."""
    logger.info("=" * 60)
    logger.info("Starting daily Claude tweet collection (via Apify)...")
    logger.info("=" * 60)

    # Run a single Apify actor call with all search terms
    raw_tweets = await run_apify_tweet_scraper(
        search_terms=SEARCH_QUERIES,
        max_items=MAX_TWEETS_PER_RUN,
    )

    logger.info(f"Total raw tweets from Apify: {len(raw_tweets)}")

    # Normalize and filter
    normalized = []
    for raw in raw_tweets:
        t = normalize_tweet(raw)
        if t:
            normalized.append(t)

    logger.info(f"After filtering: {len(normalized)} relevant tweets")

    # Deduplicate
    unique = deduplicate_tweets(normalized)
    logger.info(f"After deduplication: {len(unique)} unique tweets")

    # Sort by engagement score (descending)
    unique.sort(key=lambda t: t["engagement_score"], reverse=True)

    # Take top 5
    top5 = unique[:5]

    # Compute summary stats
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()

    avg_score = (
        round(sum(t["engagement_score"] for t in unique) / len(unique), 0)
        if unique
        else 0
    )

    top_author = top5[0]["author_handle"] if top5 else "N/A"

    # Find peak hour
    hour_counts = {}
    for t in unique:
        try:
            dt = None
            created = t.get("created_at", "")
            if created:
                for fmt in [
                    "%a %b %d %H:%M:%S %z %Y",  # Twitter/Apify format
                    "%Y-%m-%dT%H:%M:%S.%fZ",
                    "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S%z",
                ]:
                    try:
                        dt = datetime.strptime(created, fmt)
                        break
                    except ValueError:
                        continue
            if dt:
                h = dt.hour
                hour_counts[h] = hour_counts.get(h, 0) + 1
        except Exception:
            pass

    peak_hour = "N/A"
    if hour_counts:
        peak_h = max(hour_counts, key=hour_counts.get)
        peak_hour = f"{peak_h:02d}:00 UTC"

    # Determine trend (compare with yesterday)
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_data = load_daily_data(yesterday)
    trend = "flat"
    if yesterday_data and yesterday_data.get("summary"):
        prev_avg = yesterday_data["summary"].get("avg_engagement_score", 0)
        if avg_score > prev_avg * 1.05:
            trend = "up"
        elif avg_score < prev_avg * 0.95:
            trend = "down"

    result = {
        "date": today,
        "last_updated": now_iso,
        "period": "24h",
        "summary": {
            "total_tweets_analyzed": len(unique),
            "avg_engagement_score": avg_score,
            "top_author": f"@{top_author}",
            "peak_hour": peak_hour,
            "trend": trend,
        },
        "tweets": top5,
    }

    save_daily_data(today, result)
    logger.info(f"Daily collection complete. Top 5 saved for {today}")

    if top5:
        for i, t in enumerate(top5):
            logger.info(
                f"  #{i+1}: @{t['author_handle']} — score {t['engagement_score']:,.0f} — {t['text'][:80]}..."
            )

    return result


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the scheduler on app startup."""
    # Schedule daily collection
    scheduler.add_job(
        run_daily_collection,
        CronTrigger(hour=CRON_HOUR, minute=CRON_MINUTE),
        id="daily_collection",
        name="Daily Claude Tweet Collection",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started — daily collection at {CRON_HOUR:02d}:{CRON_MINUTE:02d} UTC")

    # Run initial collection if no data exists for today
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not load_daily_data(today):
        logger.info("No data for today — running initial collection...")
        try:
            await run_daily_collection()
        except Exception as e:
            logger.error(f"Initial collection failed: {e}")

    yield

    scheduler.shutdown()
    logger.info("Scheduler stopped")


app = FastAPI(
    title="Claude Viral Tweet Tracker API",
    description="Surfaces the top 5 most viral tweets about Claude (Anthropic) daily. Powered by Apify tweet-scraper.",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS — allow any origin (dashboard can be hosted anywhere)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """Health check."""
    return {
        "service": "Claude Viral Tweet Tracker",
        "status": "running",
        "version": "2.0.0",
        "data_source": "Apify apidojo/tweet-scraper",
        "next_collection": f"{CRON_HOUR:02d}:{CRON_MINUTE:02d} UTC daily",
        "available_dates": list_available_dates()[:7],
    }


@app.get("/api/tweets")
async def get_tweets(
    period: str = Query("24h", pattern="^(24h|7d|30d)$"),
    date: Optional[str] = Query(None, pattern="^\\d{4}-\\d{2}-\\d{2}$"),
):
    """
    Get top viral tweets.

    - period: 24h, 7d, or 30d
    - date: specific date (YYYY-MM-DD), defaults to today
    """
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if period == "24h":
        # Return single day's data
        data = load_daily_data(date)
        if not data:
            raise HTTPException(
                status_code=404,
                detail=f"No data for {date}. Available dates: {list_available_dates()[:5]}",
            )
        data["period"] = "24h"
        return data

    elif period in ("7d", "30d"):
        # Aggregate multiple days
        days = 7 if period == "7d" else 30
        target_date = datetime.strptime(date, "%Y-%m-%d")
        all_tweets = []

        for i in range(days):
            d = (target_date - timedelta(days=i)).strftime("%Y-%m-%d")
            day_data = load_daily_data(d)
            if day_data and "tweets" in day_data:
                all_tweets.extend(day_data["tweets"])

        # Deduplicate and re-rank
        unique = deduplicate_tweets(all_tweets)
        unique.sort(key=lambda t: t["engagement_score"], reverse=True)
        top5 = unique[:5]

        avg_score = (
            round(sum(t["engagement_score"] for t in unique) / len(unique), 0)
            if unique
            else 0
        )
        top_author = f"@{top5[0]['author_handle']}" if top5 else "N/A"

        return {
            "date": date,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "period": period,
            "summary": {
                "total_tweets_analyzed": len(unique),
                "avg_engagement_score": avg_score,
                "top_author": top_author,
                "peak_hour": "N/A",
                "trend": "flat",
            },
            "tweets": top5,
        }


@app.get("/api/dates")
async def get_dates():
    """List all available dates with data."""
    return {"dates": list_available_dates()}


@app.post("/api/collect")
async def trigger_collection(secret: Optional[str] = Query(None)):
    """Manually trigger a collection run. Optionally protect with a secret."""
    expected_secret = os.environ.get("COLLECT_SECRET", "")
    if expected_secret and secret != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid secret")

    result = await run_daily_collection()
    return {"status": "ok", "date": result["date"], "tweets_found": len(result["tweets"])}


@app.get("/api/search")
async def search_tweets(
    searchType: str = Query("keyword", pattern="^(keyword|username)$"),
    query: str = Query(..., min_length=1, max_length=200),
    timeframe: str = Query("24h", pattern="^(4h|12h|24h|3d|7d)$"),
):
    """
    Search for viral tweets by keyword or username with timeframe filtering.

    - searchType: "keyword" or "username"
    - query: the keyword to search for, or the username (without @)
    - timeframe: 4h, 12h, 24h, 3d, or 7d
    """
    logger.info(f"Search request: type={searchType}, query={query}, timeframe={timeframe}")

    # Build search terms for Apify
    if searchType == "username":
        # Strip @ if user included it
        username = query.lstrip("@")
        search_terms = [f"from:{username}"]
    else:
        # Keyword search
        search_terms = [query]

    # Run Apify actor
    raw_tweets = await run_apify_tweet_scraper(
        search_terms=search_terms,
        max_items=MAX_TWEETS_PER_RUN,
    )

    logger.info(f"Search: got {len(raw_tweets)} raw tweets from Apify")

    # Normalize tweets — skip Claude AI filter for custom searches
    normalized = []
    for raw in raw_tweets:
        t = normalize_tweet(raw, skip_claude_filter=True)
        if t:
            normalized.append(t)

    logger.info(f"Search: {len(normalized)} tweets after normalization")

    # For keyword searches, additionally filter to ensure keyword appears in text
    if searchType == "keyword":
        normalized = [t for t in normalized if keyword_matches(t["text"], query)]
        logger.info(f"Search: {len(normalized)} tweets after keyword filtering")

    # Deduplicate
    unique = deduplicate_tweets(normalized)
    logger.info(f"Search: {len(unique)} unique tweets")

    # Apply timeframe filter
    filtered = filter_by_timeframe(unique, timeframe)
    logger.info(f"Search: {len(filtered)} tweets after timeframe filter ({timeframe})")

    # Sort by engagement score descending (virality)
    filtered.sort(key=lambda t: t["engagement_score"], reverse=True)

    # Take top results
    top_tweets = filtered[:10]

    # Compute summary stats
    avg_score = (
        round(sum(t["engagement_score"] for t in filtered) / len(filtered), 0)
        if filtered
        else 0
    )
    top_author = f"@{top_tweets[0]['author_handle']}" if top_tweets else "N/A"

    return {
        "search_type": searchType,
        "query": query,
        "timeframe": timeframe,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_tweets_analyzed": len(filtered),
            "avg_engagement_score": avg_score,
            "top_author": top_author,
            "peak_hour": "N/A",
            "trend": "flat",
        },
        "tweets": top_tweets,
    }


# ---------------------------------------------------------------------------
# Content Generation — /api/generate
# ---------------------------------------------------------------------------


class GenerateRequest(BaseModel):
    tweet_text: str
    tweet_author: str = ""
    tweet_metrics: dict = {}
    platforms: list[str]
    hook_type: str
    tone: str
    controversy_level: int
    cta_style: str
    niche: str


GENERATE_SYSTEM_PROMPT = """You are an expert viral content ghostwriter optimized for X (Twitter) and LinkedIn algorithms. You generate ready-to-post content that maximizes algorithmic reach and engagement.

===== X ALGORITHM RULES =====
- Single tweets: 71-100 characters sweet spot (17% higher engagement)
- Max 2 hashtags (21% higher engagement than 3+)
- Second-person language ("you", "your") mandatory
- 6th-7th grade reading level
- End every post with reply-provoking element
- No external links in main tweet body
- Thread structure: Hook tweet → Meat → CTA to RT first tweet
- Thread length: 5-7 tweets optimal

===== LINKEDIN ALGORITHM RULES =====
- 800-1,300 characters optimal
- 3-5 hashtags max
- First 2-3 lines are everything (truncated behind "see more")
- Short 2-3 line paragraphs with white space between
- Carousel outline: 8-10 slides, 25-50 words per slide, 1 idea per slide
- End with discussion prompt to trigger 15+ word comments

===== HUMANIZATION GUARDRAILS =====
- Sentence length variation: Mix 3-8 word sentences with 15-25 word ones
- First-person requirement: At least 20% uses "I", "we", "my"
- Contractions enforced: "don't" not "do not", "it's" not "it is"
- Pattern interrupts required: At least one mid-post format break

===== FORBIDDEN PHRASES (NEVER USE) =====
"In today's digital landscape", "It's worth noting", "Furthermore", "Moreover", "leverage", "synergy", "transformative", "In conclusion", "Additionally", "game-changer", "cutting-edge", "In the ever-evolving world of", "Let's dive in", "Without further ado", "At the end of the day"

===== SPECIFICITY MANDATE =====
Every post must include at least ONE concrete number, example, or anecdote.

===== NICHE ANGLES =====

CLIPPING NICHE:
1. Distribution > Creation: "You don't need more content. You need more distribution."
2. Volume Thesis: "1 creator + 100 clippers = 100 accounts distributing your message"
3. Cost Comparison: "Facebook CPM: $10. Clipping CPM: $1-3. Same reach, 70% less spend."
4. Social Proof: "16 billion views. 62,900 creators. Zero ad spend."
5. Anti-Ad Angle: "Your community is your ad budget."
6. Speed/Velocity: "One long-form video → 50 clips → 5 platforms → 24 hours"
7. Attention Economics: "The algorithm doesn't reward creation. It rewards distribution."
8. Creator Pain Point: "You're making great content. Nobody's seeing it. That's a distribution problem."

CRYPTO/WEB3 NICHE:
1. CT Expansion: "You're trapped in CT. Clipping gets you to TikTok, Reels, Shorts."
2. Speed to Market: "Breaking alpha? Clipping gets it everywhere in 60 seconds."
3. Community as Distribution: "Turn your holders into your marketing team."
4. Influencer Alternative: "$50K for one influencer post. Or $5K for 100 clippers posting for a month."

===== HOOK TYPES =====
- statistic: Lead with a specific number ("I analyzed 12,847...")
- contrarian: Open with disagreement ("Everyone says X. They're wrong.")
- vulnerability: Personal admission ("I got fired 3 months ago...")
- question: Provocative question ("What am I missing?")
- transformation: Before/after ("From $0 to $1M in 8 months")
- curiosity_gap: Tease value ("The thing nobody talks about...")
- pattern_interrupt: Unexpected opener ("Stop creating content.")
"""


# ---------------------------------------------------------------------------
# X Article (Viral Blog) System Prompt
# ---------------------------------------------------------------------------

X_ARTICLE_SYSTEM_PROMPT = """You are an expert viral X Article (long-form blog) ghostwriter. You write in the exact style that gets 100K-1.7M views on X Articles about clipping, content distribution, UGC, and scaling apps with creators.

===== FORMAT RULES =====
X Articles are X's long-form publishing feature. They look like blog posts inside the X app. The article must be:
- 2,500-5,000 words total
- Structured with 6-8 bold section headings
- Every section 300-600 words
- An image/graphic is expected between EVERY section (you will provide AI image generation prompts for each)

===== VOICE & TONE =====
- ALL LOWERCASE text. No capitalization except for brand names, acronyms, and the word "I"
- No emojis anywhere in the body text
- Conversational but authoritative — like you're explaining to a smart friend over coffee
- Short punchy sentences mixed with longer explanatory ones
- Heavy use of line breaks between paragraphs (double space)
- First person throughout ("I", "my", "we")
- Contractions always ("don't", "isn't", "that's", "you're")
- Informal punctuation — dashes instead of semicolons, fragments OK

===== TITLE FORMAT =====
Title MUST follow one of these patterns:
- "how to ACTUALLY [do X]..." (ACTUALLY in caps for emphasis)
- "how I made $[amount] in [timeframe] [doing X] (and how you can start this week)"
- "the [number] ways [audience] are [achieving result] (step by step guide)"
- "[bold claim]... (a complete breakdown)"
Always end the title with "..." (ellipsis)

===== STRUCTURE PATTERN =====
Every article must follow this skeleton:

1. HOOK SECTION (no heading — just the opening)
   - Open with a personal anecdote, bold claim, or shocking stat
   - Establish credibility with specific numbers (revenue, views, downloads)
   - Preview what the reader will learn
   - Include a proof screenshot/analytics reference (image prompt)
   - 200-400 words

2. CONTEXT/PROBLEM SECTION
   - Explain why the old way is broken
   - Use specific cost comparisons (paid ads vs organic, flat fees vs performance-based)
   - Reference real industry shifts
   - 300-500 words

3-5. METHOD SECTIONS (3 sections, each one step/strategy)
   - Each has a bold heading
   - Each explains one concrete tactic or system
   - Include specific tools, numbers, and processes
   - Reference real platforms: Content Rewards, Apify, Claude, CapCut, Canva, ElevenLabs, TikTok, etc.
   - "the math" — always include a calculation showing unit economics
   - 300-600 words each

6. SCALING SECTION
   - How to go from first results to serious scale
   - System/automation angle
   - "the gap between $X and $Y isn't [obvious thing]. it's [infrastructure/system]"
   - 300-500 words

7. CLOSING SECTION
   - Honest assessment ("this isn't passive from day one")
   - Summary of the full system
   - Soft CTA — mention Content Rewards naturally, not as an ad
   - "the tools are ready. the only variable is whether you start now or wait."
   - 200-300 words

===== ENGAGEMENT MECHANICS =====
- First paragraph must hook within 2 sentences (X truncates article previews)
- Include at least 3 specific dollar amounts or stats
- Use "the math" or "the numbers" as a section device — readers love seeing economics broken down
- Every section should teach something actionable
- Weave in Content Rewards / Lumina naturally as infrastructure, NOT as an ad
- End sections with a transition sentence that pulls into the next section

===== WHAT MAKES THESE GO VIRAL =====
- They look like genuine knowledge sharing, not marketing
- They include real numbers and proof (analytics screenshots, revenue figures)
- They provide a complete system, not just tips
- They name specific tools and show how they connect
- Heavy bookmark rate (readers save for later) — this is the #1 signal for X Article distribution
- The title alone should make someone stop scrolling

===== FORBIDDEN =====
- Capital letters in body text (except brand names, "I", acronyms)
- Emojis
- Generic advice without numbers
- Sounding like a marketing blog
- Using phrases like "game-changer", "leverage", "synergy", "transformative"
- Starting sentences with "Additionally", "Furthermore", "Moreover"
- Bullet points for the main content (use flowing paragraphs — bullets OK only for lists of tools/steps)

===== IMAGE PROMPT RULES =====
For each section, provide an AI image generation prompt that creates:
- Dark background (black or very dark grey) with accent colors (orange, teal, or white text)
- Bold headline text overlaid on the image summarizing that section's key stat or claim
- Sketch/illustration style or clean infographic style — NOT photorealistic
- 16:9 aspect ratio, suitable for X Article inline images
- Include specific numbers/stats in the image text where possible
- Style reference: clean data slides, startup pitch deck aesthetics
"""


def build_article_user_message(request) -> str:
    """Build the user message for X Article generation."""
    metrics = request.tweet_metrics
    likes = metrics.get("likes", 0)
    retweets = metrics.get("retweets", 0)
    impressions = metrics.get("impressions", 0)

    return f"""Generate a COMPLETE viral X Article (long-form blog post) inspired by this high-performing tweet:

Source tweet: "{request.tweet_text}"
Author: @{request.tweet_author}
Metrics: {likes} likes, {retweets} RTs, {impressions} impressions

Parameters:
- Hook type: {request.hook_type}
- Tone: {request.tone}
- Controversy level: {request.controversy_level}/5
- CTA style: {request.cta_style}
- Niche: {request.niche}

Write the full X Article with all sections. For EACH section, also provide an AI image generation prompt.

Return as JSON with this exact structure:
{{
  "generations": [
    {{
      "platform": "x_article",
      "variation": 1,
      "title": "how to ACTUALLY...",
      "sections": [
        {{
          "heading": "" or "section heading here",
          "content": "the full section text (300-600 words, lowercase, no emojis)",
          "image_prompt": "detailed AI image generation prompt for this section's graphic"
        }}
      ],
      "content": "THE COMPLETE ARTICLE TEXT — all sections concatenated with headings in bold markdown (**heading**). This is the copy-paste ready version.",
      "word_count": 3500,
      "best_time": "Tue/Wed 7-9am EST",
      "expected_engagement": "High bookmark rate — comprehensive system breakdown"
    }}
  ]
}}

Generate exactly 1 complete article (these are long — 1 is the right amount).
The 'content' field must contain the FULL article as one continuous text block with **bold headings** that can be copy-pasted directly into X's article editor.
Each section in 'sections' array should also have its own content for the structured view.
"""


@app.post("/api/generate")
async def generate_content(request: GenerateRequest):
    """
    Generate viral content variations for a given tweet across selected platforms.
    Returns 3 variations per selected platform using Claude claude-sonnet-4-20250514.
    """
    # --- Validate API key ---
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY is not configured on the server.",
        )

    # --- Validate inputs ---
    if not request.platforms:
        raise HTTPException(status_code=422, detail="platforms list must not be empty.")

    if not (1 <= request.controversy_level <= 5):
        raise HTTPException(
            status_code=422,
            detail=f"controversy_level must be between 1 and 5, got {request.controversy_level}.",
        )

    # --- Determine if this is an X Article request ---
    is_article = "x_article" in request.platforms
    non_article_platforms = [p for p in request.platforms if p != "x_article"]

    # --- Build user message ---
    if is_article:
        # Use the dedicated X Article prompt (long-form, higher token limit)
        system_prompt = X_ARTICLE_SYSTEM_PROMPT
        user_message = build_article_user_message(request)
        max_tokens = 8192  # Articles are long
    else:
        system_prompt = GENERATE_SYSTEM_PROMPT
        max_tokens = 4096

    if not is_article or non_article_platforms:
        # Build standard user message for non-article platforms
        metrics = request.tweet_metrics
        likes = metrics.get("likes", 0)
        retweets = metrics.get("retweets", 0)
        impressions = metrics.get("impressions", 0)

        platforms_to_generate = non_article_platforms if is_article else request.platforms

        if platforms_to_generate:
            standard_user_message = f"""Generate viral content based on this high-performing tweet:

Source tweet: "{request.tweet_text}"
Author: @{request.tweet_author}
Metrics: {likes} likes, {retweets} RTs, {impressions} impressions

Parameters:
- Platforms: {platforms_to_generate}
- Hook type: {request.hook_type}
- Tone: {request.tone}
- Controversy level: {request.controversy_level}/5
- CTA style: {request.cta_style}
- Niche: {request.niche}

For EACH selected platform, generate exactly 3 variations.

Return as JSON with this structure:
{{
  "generations": [
    {{
      "platform": "x_single",
      "variation": 1,
      "content": "the actual post text here",
      "best_time": "Tue/Thu 8-10am EST",
      "expected_engagement": "High reply rate — provocative question ending"
    }}
  ]
}}"""
            if not is_article:
                user_message = standard_user_message

    # --- Call Claude API ---
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[
                {"role": "user", "content": user_message}
            ],
        )
        response_text = message.content[0].text
    except anthropic.APIError as e:
        logger.error(f"Claude API error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Claude API error: {str(e)}",
        )
    except Exception as e:
        logger.error(f"Unexpected error calling Claude API: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error calling Claude API: {str(e)}",
        )

    # --- Parse JSON from response (may be wrapped in markdown code blocks) ---
    try:
        # Strip markdown code fences if present
        cleaned = response_text.strip()
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
        if match:
            cleaned = match.group(1).strip()
        else:
            # Try to find raw JSON object
            json_match = re.search(r"(\{[\s\S]*\})", cleaned)
            if json_match:
                cleaned = json_match.group(1).strip()

        parsed = json.loads(cleaned)
        generations = parsed.get("generations", [])
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse Claude response as JSON: {e}\nRaw response: {response_text[:500]}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to parse Claude response as JSON: {str(e)}",
        )

    return {
        "source_tweet": request.tweet_text,
        "generations": generations,
        "parameters": {
            "hook_type": request.hook_type,
            "tone": request.tone,
            "controversy_level": request.controversy_level,
            "cta_style": request.cta_style,
            "niche": request.niche,
        },
    }


@app.get("/health")
async def health():
    """Simple health check for uptime monitors."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
