# Deploy on Render's free plan

## What free can and cannot do

Render free web services sleep after **15 minutes without inbound requests**. Outgoing Telegram polling is not inbound traffic. The application cannot disable this platform rule.

An external uptime monitor requesting `/health` every **5 minutes** supplies regular inbound traffic and can reduce idle spin-down. This is best-effort: monitor outages, Render restarts, traffic restrictions, and free-plan quota limits can still interrupt the bot. It is not a 24/7 guarantee. Render's internal deployment health checks are not a substitute for an external monitor. Do not monitor `/robots.txt`, which Render can answer without waking the service.

The free allowance is **750 instance hours per workspace per month**, shared across services. A continuously running bot can use almost all of that. Free services can restart at any time.

Deadline alerts run only while this process is running. Saved overdue jobs are escalated after it resumes, but an alert can be late during an outage. A paid always-on service is required if reliable scheduled operation is essential.

Official source: [Render free service limits](https://render.com/docs/free).

## 1. Create a durable database

Use an external PostgreSQL database, for example a free Neon project. Copy its **direct Postgres connection string with SSL enabled**. Keep the password private. Check the database provider's current quotas: this bot polls frequently and will keep its database active, so an external free database may exhaust compute quotas before the end of the month.

Do not put credentials in GitHub. Store them in Render's environment variables and locally in `.env` only when migrating. Render's own free Postgres expires after **30 days**, so it is unsuitable as a permanent database without an upgrade.

On Render the bot refuses to use local SQLite, because the free filesystem is erased on restarts, redeploys, and spin-down. PostgreSQL keeps editor registration, assignments, deadlines, report snapshots, and delivery records durable.

## 2. Preserve your existing setup

Before the first Render deployment starts:

1. Stop the local bot with Ctrl+C.
2. Back up the entire `data` folder locally.
3. Add `DATABASE_URL=your-postgres-connection-string` to your local `.env`. Do not share or commit this file.
4. Run this once in the project folder:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe migrate_database.py
```

The migration copies your group connections, editor roster, jobs, audit history, and pending work. It refuses to overwrite a nonempty destination. It does not change the local SQLite database. Only use the migration before starting the Render service.

If you want to start fresh instead, skip migration and bind the two groups and register editors again after deploying. Stop the local bot either way; two independent databases polling the same Telegram bot will conflict.

## 3. Create the Render service

In Render, choose **New → Blueprint**, connect the private GitHub repository, and use `render.yaml`. The blueprint selects a **free Python web service**, not a paid background worker.

Provide the required secrets:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your existing token from local `.env` |
| `ADMIN_IDS` | Your admin user IDs, comma-separated |
| `DATABASE_URL` | Your external Postgres connection string |

Defaults: Asia/Kolkata timezone, 24-hour deadline, two active jobs per editor, effort-based assignment, approval required, Monday reports at 09:00.

For manual Web Service creation, use these settings:

- Runtime: Python 3.12
- Build command: `pip install -r requirements.txt`
- Start command: `python -m video_bot`
- Instance type: Free
- Health check path: `/health`

Render provides `PORT` automatically. The bot listens on `0.0.0.0` and that port. `/health` contains only `{"status":"ok"}`; it does not expose names, jobs, IDs, or secrets. It returns 503 if the worker stops making progress.

## 4. Add external monitoring

In an external HTTP uptime-monitoring service that supports five-minute checks:

- URL: `https://YOUR-SERVICE.onrender.com/health`
- Method: GET
- Interval: 5 minutes
- Expected status: 200
- Enable failure notifications to yourself.

You need to create this monitor after Render gives you the service URL. The code does not create a monitor or guarantee that the provider will never suspend the service. Do not run a self-ping loop: a sleeping process cannot wake itself.

## 5. Verify

1. Open `/health` and confirm `{"status":"ok"}`.
2. Send `/health` in the Editors group; confirm the bot responds.
3. Send `/editors` and `/jobs` to confirm migrated records exist.
4. Upload one test video and complete its submission/approval.
5. Check the monitor from another device after the PC is shut down.

During a rolling deploy, a database lease allows only one cloud instance to poll/send at a time. Graceful shutdown releases it; after an abrupt process death, takeover can take up to five minutes. Telegram pending updates are retained for at most 24 hours, so inspect group history following a longer outage.

## Files and credentials

`.env`, `data/`, `.venv/`, and `reports/` are excluded from Git. Weekly images lost on a Render restart are rebuilt from saved database snapshots before sending. GitHub contains code and setup instructions only.
