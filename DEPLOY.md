# Claude Viral Tweet Tracker — Deployment Guide

Complete step-by-step instructions to get this running in under 15 minutes.

---

## What You're Deploying

| Component | What it does | Where it runs | Cost |
|-----------|-------------|---------------|------|
| **Backend API** | Collects tweets via Apify every 24h, ranks them, serves data | Render (Free tier) | $0/mo + ~$0.20/mo Apify |
| **Frontend Dashboard** | Shows the top 5 viral tweets in a clean UI | Vercel (Free tier) OR any static host | $0/mo |

**Total monthly cost: ~$0.20** (Apify charges $0.40 per 1,000 tweets; you'll collect ~500/day = ~$0.20/month)

---

## Prerequisites

- A GitHub account (free)
- An Apify account (sign up at https://apify.com — $5/month free credits on the Free plan)

That's it. No credit card needed for Render or Vercel free tiers.

---

## Step 1: Get Your Apify API Token (2 minutes)

1. Go to **https://apify.com** and sign up (or log in)
2. Click your profile icon → **Settings**
3. Go to **Integrations** → **API tokens**
4. Click **+ Create token** → give it a name like "claude-tracker"
5. Copy your **API token**
6. Save it somewhere — you'll need it in Step 3

> **Why Apify?** The official X/Twitter API v2 costs $200/month minimum for search. Apify's `apidojo/tweet-scraper` actor gives you reliable tweet search for $0.40 per 1,000 tweets — well within the free tier credits.

---

## Step 2: Push Code to GitHub (3 minutes)

### Backend Repository

1. Go to **https://github.com/new**
2. Create a new repo called **claude-tracker-api**
3. Set it to **Private** (recommended)
4. On your machine, open a terminal:

```bash
cd claude-tracker-api
git init
git add .
git commit -m "Initial commit — Claude Viral Tweet Tracker API (Apify)"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/claude-tracker-api.git
git push -u origin main
```

### Frontend Repository

1. Create another repo called **claude-tracker**
2. Set it to **Private**

```bash
cd claude-tracker
git init
git add .
git commit -m "Initial commit — Claude Tracker Dashboard"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/claude-tracker.git
git push -u origin main
```

---

## Step 3: Deploy the Backend on Render (5 minutes)

1. Go to **https://dashboard.render.com**
2. Click **New** → **Web Service**
3. Connect your GitHub account (if not already connected)
4. Select the **claude-tracker-api** repository
5. Configure these settings:

| Setting | Value |
|---------|-------|
| **Name** | claude-tracker-api |
| **Region** | Frankfurt (EU) or Oregon (US West) — your choice |
| **Branch** | main |
| **Runtime** | Python 3 |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| **Instance Type** | Free |

6. Click **Advanced** and add a **Disk**:

| Disk Setting | Value |
|-------------|-------|
| **Name** | tweet-data |
| **Mount Path** | /opt/render/project/data |
| **Size** | 1 GB |

> **Important:** The Free tier does NOT include persistent disks. You have two options:
> - **Option A (Recommended):** Use the **Starter plan at $7/month** which includes disk and doesn't sleep after 15 minutes.
> - **Option B (Free):** Skip the disk. Data will be stored in memory and rebuilt on each restart. The scheduler will re-collect data on startup if none exists for today.

7. Add **Environment Variables**:

| Key | Value |
|-----|-------|
| `APIFY_API_TOKEN` | (paste your Apify API token from Step 1) |
| `CRON_HOUR` | `9` |
| `CRON_MINUTE` | `0` |
| `DATA_DIR` | `/opt/render/project/data` (or `./data` if no disk) |
| `LOG_LEVEL` | `INFO` |
| `COLLECT_SECRET` | (generate one: run `python -c "import secrets; print(secrets.token_hex(16))"`) |

8. Click **Create Web Service**
9. Wait 2-3 minutes for the deploy to complete
10. Your API will be live at: `https://claude-tracker-api.onrender.com`

### Verify the Backend

Open your browser and go to:
```
https://claude-tracker-api.onrender.com/
```

You should see a JSON response like:
```json
{
  "service": "Claude Viral Tweet Tracker",
  "status": "running",
  "version": "2.0.0",
  "data_source": "Apify apidojo/tweet-scraper",
  "next_collection": "09:00 UTC daily"
}
```

Then check for data:
```
https://claude-tracker-api.onrender.com/api/tweets?period=24h
```

If the API just started, it will have run the first collection automatically.

---

## Step 4: Deploy the Frontend on Vercel (3 minutes)

1. Go to **https://vercel.com**
2. Sign in with GitHub
3. Click **Add New** → **Project**
4. Import the **claude-tracker** repository
5. Configure:

| Setting | Value |
|---------|-------|
| **Framework Preset** | Other |
| **Root Directory** | ./ |
| **Build Command** | (leave empty) |
| **Output Directory** | ./ |

6. Click **Deploy**
7. Your dashboard will be live at: `https://claude-tracker.vercel.app` (or similar)

### Connect the Dashboard to the API

1. Open the dashboard in your browser
2. Click the **gear icon** (⚙️) in the top right
3. In the **API Endpoint** field, paste your Render URL:
   ```
   https://claude-tracker-api.onrender.com
   ```
4. Click **Apply**
5. The dashboard will now fetch real data from your API

### Make It Permanent

Since the dashboard can't use localStorage (sandboxed), you can hardcode the API URL:

1. Open `app.js` in your code editor
2. Find this line near the top:
   ```javascript
   const CONFIG = {
     API_URL: '',
   ```
3. Change it to:
   ```javascript
   const CONFIG = {
     API_URL: 'https://claude-tracker-api.onrender.com',
   ```
4. Commit and push — Vercel auto-deploys the update

---

## Step 5: Verify Everything Works (2 minutes)

1. **Dashboard loads**: Open your Vercel URL — you should see the KPI bar and tweet cards
2. **Data is real**: The tweets should be actual Claude-related tweets from Twitter
3. **Historical nav works**: Click the left arrow to see previous days (only today's data will exist initially; it builds up over time)
4. **Period toggle works**: Switch between 24h, 7d, 30d views
5. **API health**: Visit `https://claude-tracker-api.onrender.com/health`

---

## How It Works — Daily Automation

Every day at **9:00 AM UTC** (configurable via `CRON_HOUR`), the backend automatically:

1. Runs the Apify `apidojo/tweet-scraper` actor with 6 Claude-related search terms
2. Collects up to 500 tweets in a single actor run
3. Filters out non-AI tweets (people named Claude, GTA characters, etc.)
4. Filters out low-follower accounts (< 100 followers) to remove bots
5. Deduplicates across all queries
6. Computes engagement score for each tweet
7. Ranks and saves the top 5
8. Stores the results as a JSON file for historical access

**You never need to trigger it manually.** Just leave the Render service running.

---

## Manual Collection

If you want to trigger a collection outside the schedule:

```bash
curl -X POST "https://claude-tracker-api.onrender.com/api/collect?secret=YOUR_COLLECT_SECRET"
```

Or open this URL in your browser (replacing with your secret):
```
https://claude-tracker-api.onrender.com/api/collect?secret=YOUR_COLLECT_SECRET
```

---

## Customization

### Change the Collection Time

Edit the environment variables on Render:
- `CRON_HOUR=14` → Collects at 2:00 PM UTC
- `CRON_HOUR=5` and `CRON_MINUTE=0` → 5:00 AM UTC (9:00 AM Dubai)

### Change Search Queries

Edit the `SEARCH_QUERIES` list in `main.py` to add/remove search terms. These use Twitter advanced search syntax.

### Change the Engagement Formula

Edit the `WEIGHTS` dictionary in `main.py`:
```python
WEIGHTS = {
    "impressions": 0.3,  # Weight impressions (reach)
    "retweets": 10,      # Weight retweets highest (amplification)
    "likes": 3,          # Standard like weight
    "replies": 5,        # Replies indicate conversation
    "quotes": 8,         # Quote tweets = strong signal
    "bookmarks": 6,      # Bookmarks = save-worthy content
}
```

### Change Minimum Followers Filter

Edit `MIN_FOLLOWERS` in `main.py` (default: 100). Set higher to filter more aggressively.

---

## Cost Breakdown

| Service | Plan | Monthly Cost |
|---------|------|-------------|
| Render | Free (or Starter $7/mo for persistent disk) | $0 or $7 |
| Vercel | Free (Hobby) | $0 |
| Apify | Free plan ($5 credits) or pay-per-use | ~$0.20 |
| **Total** | | **$0.20 — $7.20/month** |

---

## Troubleshooting

### "No data for today"
The first collection runs on startup. If you just deployed, wait 2-5 minutes for the Apify actor to finish. Check the Render logs for progress.

### Dashboard shows mock data
You haven't connected the API yet. Click the gear icon and paste your Render API URL.

### Render service sleeps (Free tier)
Free tier services sleep after 15 minutes of inactivity. The first request after sleeping takes 30-60 seconds to wake up. The daily cron will still run because APScheduler runs inside the app process — but it needs the app to be awake. Solution: use an external uptime monitor like https://uptimerobot.com (free) to ping your `/health` endpoint every 14 minutes.

### Apify errors
Check your API token is correct. Check your usage at https://console.apify.com/billing. The `apidojo/tweet-scraper` actor costs $0.40 per 1,000 tweets. The Apify free plan includes $5/month in credits, which is enough for ~12,500 tweets/month (well over what this tool needs).

### Apify actor run timing out
The actor typically takes 30-120 seconds per run. If it's timing out (>5 min), check the actor run status at https://console.apify.com/actors/runs. You may need to reduce `MAX_TWEETS_PER_RUN` in `main.py`.

---

## Folder Structure

```
claude-tracker-api/          ← Backend (deploy to Render)
├── main.py                  ← Full API server + Apify integration + scheduler
├── requirements.txt         ← Python dependencies
├── render.yaml              ← Render deployment blueprint
├── Procfile                 ← Process file
├── .env.example             ← Environment variable template
├── .gitignore               ← Git ignore rules
└── README.md                ← API documentation

claude-tracker/              ← Frontend (deploy to Vercel)
├── index.html               ← Dashboard HTML
├── base.css                 ← CSS reset/foundation
├── style.css                ← Design system + components
└── app.js                   ← Dashboard logic + mock data + API integration
```
