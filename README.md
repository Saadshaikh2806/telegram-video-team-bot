# Telegram Video Team Bot

Automatically assign uploaded videos to editors, track delivery, notify admins when a deadline is missed, and post weekly performance charts.

**Render deployment:** follow [RENDER_SETUP.md](RENDER_SETUP.md). Free Render needs an external Postgres database. Built-in five-minute self-ping helps keep it active, and an external uptime monitor is recommended for wake-up and outage alerts; uninterrupted operation cannot be guaranteed on the free plan.

**Status:** built and tested locally with simulated Telegram responses. A real bot token, two groups, and an always-on computer/server are required to go live. No Telegram messages have been sent during development.

## Start on this Windows computer

Dependencies are already installed in `.venv` on this computer.

1. In Telegram, open the official **@BotFather**, send `/newbot`, and create your bot. Copy its token.
2. Double-click **Start Bot.cmd** in this folder. A setup window opens: click **Paste token**, then **Save settings**. It saves credentials locally in `.env`. You can also open **Configure Bot.cmd** at any time to edit settings.
3. If you do not know your numeric Telegram user ID, leave admin IDs blank. Start the bot and send it `/whoami` in a private chat. Stop it with Ctrl+C, open **Configure Bot.cmd**, add your number to the admin IDs field, save, then start it again. Separate multiple admin IDs with commas.
4. Create the **Video Uploaders** and **Video Editors** groups, or use your existing groups. Add the bot to both. Make it an administrator so it can receive ordinary uploads and replies. Allow it to send messages, videos, photos, and documents. It does not need permission to delete messages or manage members.
5. From your personal Telegram account listed in `ADMIN_IDS`, send `/bind_uploaders` inside the Uploaders group and `/bind_editors` inside the Editors group. Anonymous admin messages are not accepted for administrative actions.
6. Ask each editor to send `/join` in the Editors group from their personal account. Tap **Approve** on the bot's request message. Only configured admins can approve. Alternatively, reply to an editor's ordinary message with `/add_editor`; if Telegram omits their identity from the reply, use `/join` instead. Only explicitly registered editors receive assignments.
7. Upload a short test video in Uploaders with the caption `Add English captions #effort1`. Check that it arrives with the assigned editor and deadline in Editors.
8. Have that editor reply to the assignment with an edited video, then have an admin approve it. Check that the approved result appears in Uploaders.

On another Windows computer with Python 3.12+, run **Install Bot.cmd** first, then **Start Bot.cmd**. Keep the bot window open and the computer awake. Closing it or putting the computer to sleep stops monitoring until it starts again.

## Daily workflow

- Upload a video with a brief and `#effort1`, `#effort2`, or `#effort3`. Without a tag, effort defaults to 2. Videos sent as documents are supported if their MIME type or extension identifies them as video.
- Use `/new https://your-source-folder-link editing instructions #effort2` for cloud folders, multi-file footage, or other source links. Make sure editors can open the link.
- Albums are intentionally rejected with a helpful message so one multi-file job is not accidentally assigned to different editors.
- The editor uses **Start editing**, then replies directly to the assignment with the finished video/document. Alternatively, send `/submit 12 https://finished-video-link` or attach a video with caption `/submit 12`.
- Only the assigned editor can start or submit the job. Only configured admins can approve, extend, cancel, change availability, or request revisions.
- The bot posts the approved result back to Uploaders. Each job has a stable ID, such as `VID-0012`.

## Fair assignment rules

Default mode is **equal assigned effort**. Simple = 1, standard = 2, complex = 3. Your team should agree on examples for each category; the bot does not infer editing difficulty from duration.

1. Consider registered, available editors below the common active-job limit (default: 2).
2. Choose the editor with the lowest cumulative allocation balance.
3. Break ties by longest time since last assignment, then numeric ID for a deterministic initial tie.
4. Charge the selected editor the job's effort points as soon as the slot is reserved.

This balances work among eligible editors over time, rather than resetting everyone to zero each Monday. Counts can differ when effort, availability, or capacity differs. Editors who are at capacity are skipped; waiting jobs are assigned when capacity opens. Review-pending jobs count toward the limit to prevent unlimited unfinished review queues.

New editors start at the current minimum allocation balance, so they do not receive a large catch-up backlog. When an admin reactivates an editor after leave, their balance is raised to at least the current minimum. Existing jobs and deadlines remain active during leave. Cancelling a job before editing begins refunds its allocation charge; cancellation after work starts does not erase effort already allocated.

For equal **video count**, set `ASSIGNMENT_MODE=rotation` before initial use. The same availability/capacity rules apply, with each job charged as 1. Do not switch modes on an existing team's database without recalculating its balances.

## Deadlines and revisions

- Default: 24 elapsed hours, including weekends, starting when the bot posts the assignment. Pressing Start does not reset it.
- Reminders: six hours and two hours before the deadline; one admin escalation when the deadline is reached without submission.
- Pending notices are checked again before sending, so a queued overdue notice is skipped after submission or a deadline extension.
- A blocked job still has a running clock. An admin can use `/extend 12 4 Waiting for missing footage`. The extension is recorded in the audit history.
- A submitted job stops editor deadline alerts while awaiting review.
- `/revise 12 Fix caption spelling` creates a new 24-hour revision deadline and records a revision. The first submission timestamp remains available for reporting.
- Submissions use Telegram's message timestamp, bounded to the assignment/processing times, so a polling delay does not make an on-time submission late.
- `REQUIRE_APPROVAL=false` allows submission to complete a job immediately. Approval is enabled by default.

## Weekly report

Every Monday at **09:00 Asia/Kolkata**, the bot posts charts and a CSV in Editors for the previous Monday–Sunday. Timezone and report hour are configurable. `/report` requests the previous week manually. The first automatic report arrives on the Monday after setup; subsequent missed reporting periods are caught up after a restart.

Reports include assigned video count, assigned effort, approved video count, approved effort, first-delivery on-time rate, and first-pass approval rate. The PNG is readable in Telegram; the CSV can be opened in Excel.

- **On-time:** first submissions on or before the approved first-delivery deadline divided by all non-cancelled jobs due that week. Missing submissions count as late. Extensions approved before the first submission adjust that first-delivery deadline and remain in the audit log.
- **Quality:** approvals without a requested revision divided by all approvals that week.
- **Most reliable:** best on-time percentage among editors with at least three jobs due; ties are shared.
- **Best first-pass quality:** best percentage among editors with at least three approvals; ties are shared.
- **Highest completed effort:** most approved effort points, with ties shared.

Delivery timing measures the first submitted draft; quality is a separate measure. The bot does not inspect whether a file is a good edit. Admin review is necessary for that. Historical reports regenerated later can reflect later approvals, cancellations, or extensions. Existing chart/CSV files remain as generated snapshots.

The sample in `reports/sample-performance-1.png` uses fictional data and does not represent your team.

## Commands

| Command | Who | Purpose |
|---|---|---|
| `/whoami` | Anyone | Show their own user ID and current chat ID |
| `/help` | Anyone | Show commands |
| `/bind_uploaders`, `/bind_editors` | Admin | Connect the two groups |
| `/unbind_uploaders`, `/unbind_editors` | Admin | Undo an incorrect binding inside that group before any jobs exist; editor registrations are preserved |
| `/add_editor` as a reply | Admin | Register an editor |
| `/join` | Editor | Request registration, with an admin approval button |
| `/add_editor USER_ID` | Admin | Approve someone who already sent `/join` |
| `/availability 12345 off` or `on` | Admin | Pause/resume new assignments |
| `/myjobs` | Editor | Show latest 30 open assignments |
| `/job 12` | Group member | Show a job |
| `/start_job 12` | Assigned editor | Mark editing started |
| `/submit 12 https://link` | Assigned editor | Submit finished work |
| `/block 12 reason` | Assigned editor | Notify admins of a blocker |
| `/approve 12` | Admin | Complete a submitted job |
| `/revise 12 feedback` | Admin | Return a submitted job for changes |
| `/extend 12 4 reason` | Admin | Add 4 hours to the current deadline |
| `/cancel 12 reason` | Admin | Close a job without completion |
| `/jobs` | Admin | Show latest 30 open jobs |
| `/editors` | Admin | Show roster and allocation balances |
| `/report` | Admin | Generate the previous week's report |
| `/health` | Admin | Show job queue and message-delivery failures |
| `/retry` | Admin | Retry failed outgoing messages after fixing access |

Admin IDs are an explicit local allowlist, not every Telegram group administrator. Add the admins you want tagged to `ADMIN_IDS`; they should belong to the Editors group. Mentions cannot override someone's notification settings.

## Hosting and reliability

For 24/7 operation, use an always-on host. A Docker configuration is included:

```sh
docker compose up -d --build
docker compose logs -f
```

Run **only one bot instance**. Do not run the Windows launcher and Docker deployment simultaneously for the same token. A pre-existing webhook is detected and startup stops rather than replacing another integration.

The database is `data/bot.sqlite3`; reports are in `reports/`. Docker persists both folders. To back up simply, stop the bot, copy the entire `data` folder plus `.env`, then restart. Keep the backup private. Never delete the database to troubleshoot a running team: it holds assignments, balances, and deadlines.

Incoming update handling, state changes, and outgoing message creation are saved transactionally. Duplicate Telegram updates do not repeat database actions; duplicate source file identifiers or exact source links do not create a second job. The same link with a different URL is not detected as identical content.

Outgoing messages retry transient failures and Telegram rate limits. Permanent access/content errors are retained for `/health` and `/retry`; failed assignments do not start an editor deadline. Sending is paced per group. At peak load, messages can arrive later than their scheduled threshold.

Telegram does not provide an idempotency key for ordinary sends. A process crash or lost network response immediately after Telegram accepts a send can produce a duplicate message when retried. The database still retains one job; exactly-once external delivery cannot be guaranteed.

Telegram keeps pending bot updates for at most 24 hours. Downtime beyond that can lose new uploads/submissions that the bot never received; compare with group history after a long outage. Deadlines for existing saved jobs still survive. Source messages must remain accessible until the assignment has been copied.

Large videos are copied within Telegram rather than downloaded by this application, avoiding the standard `getFile` download limit. Protected content, deleted source messages, group permissions, or platform restrictions can still prevent delivery. Use a source link if copying is unavailable.

Current scope: one uploader group and one editor group, text/link/video jobs, manual availability, approval and revisions. Automatic reassignment, forum-topic creation, review-SLA reminders, holiday calendars, skill-based routing, and a web dashboard are not included in this version. Removing someone from Telegram does not automatically remove them from the roster; set availability off first.

## Development and verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe demo.py
.\.venv\Scripts\python.exe -m video_bot --check
```

The configuration check requires a token in `.env` but makes no network calls. Automated tests use a fake Telegram client and do not send real messages. Live group permissions, delivery, and mentions must be checked during the initial test job.

Official references: [Telegram Bot API](https://core.telegram.org/bots/api) and [Bots FAQ](https://core.telegram.org/bots/faq).
