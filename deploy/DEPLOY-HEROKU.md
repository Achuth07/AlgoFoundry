# AlgoFoundry — Heroku Deployment Guide

## Budget: $13/mo (GitHub Student Pack)

| Service | Plan | Cost |
|---------|------|------|
| Dyno | Basic (always on) | $7/mo |
| Database | Mini Postgres | $5/mo |
| **Total** | | **$12/mo** |

## Prerequisites

- [Heroku CLI](https://devcenter.heroku.com/articles/heroku-cli) installed
- Git repo initialized for AlgoFoundry
- GitHub Student Pack credits applied to your Heroku account

## Step 1: Create the App

```bash
cd /path/to/AlgoFoundry
heroku login
heroku create algofoundry-app   # pick a unique name
```

## Step 2: Add Postgres

```bash
heroku addons:create heroku-postgresql:mini -a algofoundry-app
```

This sets `DATABASE_URL` automatically. The updated `db.py` detects this
and switches from SQLite to Postgres — no code changes needed.

## Step 3: Set Config Vars (Environment Variables)

```bash
heroku config:set -a algofoundry-app \
    ALGOFOUNDRY_USER=admin \
    ALGOFOUNDRY_PASSWORD=your-strong-password \
    ALGOFOUNDRY_WEBHOOK_SECRET=your-tradingview-secret \
    OPENROUTER_API=your-key \
    ALPHA_VANTAGE_API=your-key \
    FINNHUB_API=your-key \
    GROQ_API=your-key \
    GEMINI_API=your-key
```

Do NOT set `ALGOFOUNDRY_DB` — Heroku Postgres uses `DATABASE_URL` instead.

## Step 4: Deploy

```bash
git add Procfile runtime.txt requirements.txt app/db.py
git commit -m "Add Heroku deployment support with Postgres"
git push heroku main
```

## Step 5: Scale to Basic Dyno

Heroku defaults to Eco dynos. Switch to Basic (always on, no sleeping):

```bash
heroku ps:type basic -a algofoundry-app
```

## Step 6: Verify

```bash
heroku open -a algofoundry-app
heroku logs --tail -a algofoundry-app
```

## Step 7: Migrate Existing SQLite Data (Optional)

If you have existing data in `algofoundry.db` locally, export and import:

```bash
# Export from local SQLite
sqlite3 algofoundry.db ".dump settings" > settings_dump.sql
sqlite3 algofoundry.db ".dump events" > events_dump.sql

# Connect to Heroku Postgres
heroku pg:psql -a algofoundry-app

# Then manually INSERT your settings rows, or use a migration script.
# The schema is created automatically on first boot.
```

## IBKR Gateway on Heroku

Heroku dynos can't run IB Gateway (no GUI/X11). Options:

1. **SSH tunnel from your local machine** (recommended for now):
   You can't SSH into Heroku dynos for port forwarding the standard way.
   Instead, run IB Gateway locally and use a tunneling service:
   ```bash
   # Option A: Use ngrok to expose local IB Gateway
   ngrok tcp 4001
   # Then set IBKR_HOST and IBKR_PORT in Heroku config to the ngrok address

   # Option B: Use a cheap VPS (Hetzner €4.50/mo) just for IB Gateway
   # and point Heroku at its IP
   ```

2. **Set IBKR connection via config vars:**
   ```bash
   heroku config:set -a algofoundry-app \
       IBKR_HOST=your-gateway-host \
       IBKR_PORT=4001
   ```
   (You'll need to update `broker.py` to read these from env if it doesn't already.)

## Important Heroku Caveats

### Daily Dyno Restart
Heroku restarts all dynos every ~24 hours. This means:
- Your IB connection will drop once per day
- The app's `ib_async` reconnection logic should handle this
- APScheduler in-memory jobs are lost on restart (they re-initialize from DB on boot)

### No Local Filesystem
- SQLite won't work — that's why we use Postgres
- Any files written to disk are lost on restart
- Logs go to `heroku logs`, not a file

### 512MB RAM Limit
Your app should stay well under this. If you see R14 (Memory quota exceeded)
errors in logs, reduce Uvicorn workers from 2 to 1 in the Procfile.

## Common Commands

```bash
# View logs
heroku logs --tail -a algofoundry-app

# Restart
heroku restart -a algofoundry-app

# Run one-off command
heroku run python -c "from app.db import init_db; init_db()" -a algofoundry-app

# Check dyno status
heroku ps -a algofoundry-app

# Check database
heroku pg:info -a algofoundry-app
heroku pg:psql -a algofoundry-app

# View config vars
heroku config -a algofoundry-app
```

## Deploy Updates

```bash
git add .
git commit -m "your changes"
git push heroku main
```

Heroku auto-detects `requirements.txt`, installs dependencies, and restarts.
