# Claude Viral Tweet Tracker — API

Backend API that collects the top 5 most viral tweets about Claude (Anthropic) every 24 hours using the Apify `apidojo/tweet-scraper` actor.

## Files

| File | Purpose |
|------|---------|
| `main.py` | The entire backend — FastAPI server, Apify tweet-scraper integration, engagement ranking, scheduler, data storage |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment variable template |
| `render.yaml` | Render.com deployment blueprint |
| `Procfile` | Process file for deployment |
| `DEPLOY.md` | Full deployment walkthrough |

## Setup

1. Get an Apify API token at https://apify.com → Settings → Integrations → API tokens
2. Copy `.env.example` to `.env` and fill in your `APIFY_API_TOKEN`
3. `pip install -r requirements.txt`
4. `uvicorn main:app --reload`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check + available dates |
| `GET` | `/api/tweets?period=24h&date=2026-03-01` | Get top 5 tweets for a date/period |
| `GET` | `/api/dates` | List all dates with data |
| `POST` | `/api/collect?secret=xxx` | Manually trigger collection |
| `GET` | `/health` | Simple health check |

## How It Works

1. The Apify `apidojo/tweet-scraper` actor is called with 6 Claude-related search terms
2. Up to 500 tweets are collected per run (configurable via `MAX_TWEETS_PER_RUN`)
3. Tweets are filtered for AI context and minimum follower count
4. Engagement scores are computed using the weighted formula
5. Top 5 are saved as a JSON file for that day

## Engagement Score Formula

```
score = (impressions × 0.3) + (retweets × 10) + (likes × 3) + (replies × 5) + (quotes × 8) + (bookmarks × 6)
```

## Data Storage

Tweet data is stored as JSON files in the `data/` directory:
- `data/2026-03-01.json` — one file per day
- Each file contains the top 5 tweets + summary stats
- Historical data accumulates automatically

No database required. At ~2KB per day, a 1GB disk holds 500,000+ days of data.
