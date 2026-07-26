# AlgoFoundry — Heroku Operations Cheat-Sheet

Day-to-day reference for the live Heroku deployment. For first-time setup, see
[DEPLOY-HEROKU.md](DEPLOY-HEROKU.md).

## The deployment at a glance

| Service | Plan | Cost | Notes |
|---------|------|------|-------|
| Web dyno | Basic (always on) | $7/mo | `uvicorn app.main:app --workers 2` |
| Heroku Postgres | essential-0 | $5/mo | attached as `DATABASE` → `DATABASE_URL` |
| **Total** | | **~$12/mo** | metered hourly, capped at these maximums |

- **App name:** `algofoundry-app`
- **URL:** https://algofoundry-app-60828124cd1e.herokuapp.com/
- No Redis / worker / scheduler add-ons — just the dyno + Postgres.
- The dashboard is HTTP Basic-auth protected (`ALGOFOUNDRY_USER` / `ALGOFOUNDRY_PASSWORD`).

## How deploys work

Heroku builds from git. `git push heroku main` reads `requirements.txt`,
installs deps, builds a slug, and does a zero-downtime release. `Procfile`
says what to run; `runtime.txt` pins the Python version. Data lives in
Postgres because the dyno filesystem is wiped on every restart (~daily).

`origin` (GitHub) and `heroku` are **separate remotes** — pushing to one does
not update the other. Push to both.

## Deploy a code change

```bash
git add .
git commit -m "describe your change"
git push origin main      # source of truth (GitHub)
git push heroku main      # rebuild + restart the live app
```

Watch it come up:

```bash
heroku logs --tail -a algofoundry-app
```

## Change a secret / API key (no code push needed)

Setting a config var restarts the dyno automatically; a git push is not needed.

```bash
heroku config:set -a algofoundry-app FINNHUB_API=your-new-key
heroku config:set -a algofoundry-app ALGOFOUNDRY_PASSWORD='new-strong-password'
```

Config vars the app reads: `ALGOFOUNDRY_USER`, `ALGOFOUNDRY_PASSWORD`,
`ALGOFOUNDRY_WEBHOOK_SECRET`, `ALPHA_VANTAGE_API`, `FINNHUB_API`,
`MASSIVE_API` (or `POLYGON_API`). `DATABASE_URL` is managed by the Postgres
add-on — do not set it by hand. Do **not** set `ALGOFOUNDRY_DB` (SQLite only).

## Everyday commands

```bash
# Status & logs
heroku ps -a algofoundry-app             # is the dyno up?
heroku logs --tail -a algofoundry-app    # live logs
heroku logs -n 200 -a algofoundry-app    # last 200 lines

# Config
heroku config -a algofoundry-app         # list all config vars (shows values!)
heroku config:get FINNHUB_API -a algofoundry-app

# Control
heroku restart -a algofoundry-app        # restart without deploying
heroku open -a algofoundry-app           # open the app in a browser

# Database
heroku pg:info -a algofoundry-app        # plan, status, size, row count
heroku pg:psql -a algofoundry-app        # interactive SQL shell

# One-off command in a temporary dyno
heroku run "python -c 'from app import db; db.init_db()'" -a algofoundry-app
```

## Health checks

```bash
# App health (public, no auth) — expect {"ok":true,...}
curl https://algofoundry-app-60828124cd1e.herokuapp.com/health

# Authenticated root — expect HTTP 200
curl -u "$USER:$PASS" https://algofoundry-app-60828124cd1e.herokuapp.com/
```

`"connected":false` in `/health` refers to the **IBKR broker** connection,
which is expected on Heroku — dynos can't run IB Gateway (no GUI). The web
app itself is up regardless. See DEPLOY-HEROKU.md for tunnelling options.

## Rollback

If a deploy breaks the app, roll back to the previous release:

```bash
heroku releases -a algofoundry-app        # list releases (v1, v2, ...)
heroku rollback -a algofoundry-app        # revert to the previous release
heroku rollback v6 -a algofoundry-app     # revert to a specific version
```

## Gotchas

- **Daily dyno restart** — Heroku cycles dynos ~every 24h. In-memory
  APScheduler jobs are lost and re-initialised from the DB on boot.
- **512 MB RAM** — if you see `R14` (memory quota exceeded) in logs, drop
  `--workers 2` to `--workers 1` in the `Procfile`.
- **Ephemeral filesystem** — anything written to disk is gone on restart.
  Persist to Postgres.
- **Multiple workers + schema init** — the schema build is guarded by a
  Postgres advisory lock in `app/db.py` so concurrent workers don't race on
  first boot. Keep that lock if you touch `_init_db_pg()`.
