import html
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta

from .reports import build_report, recognition
from .telegram import TelegramError


log = logging.getLogger(__name__)


class Runner:
    def __init__(self, engine, api):
        self.e, self.api = engine, api
        self.chat_next = {}

    def flush(self, limit=15):
        now = self.e.clock()
        rows = self.e.db.execute("SELECT * FROM outbox WHERE state='pending' AND available_at<=? ORDER BY id LIMIT 100", (now,)).fetchall()
        sent = 0
        blocked_chats = set()
        for row in rows:
            if sent >= limit:
                break
            payload = json.loads(row['payload'])
            chat = payload.get('chat_id', self.e.editors_chat if row['method'] == 'assignment' else 0)
            if chat and (chat in blocked_chats or self.chat_next.get(chat, 0) > now):
                blocked_chats.add(chat)
                continue
            try:
                self.deliver(row, payload)
                sent += 1
                if chat:
                    self.chat_next[chat] = self.e.clock() + 3.1
            except TelegramError as exc:
                permanent = exc.code in (400, 401, 403, 404)
                attempts = row['attempts'] + 1
                delay = max(exc.retry_after, min(300, 2 ** min(attempts, 8)))
                with self.e.db:
                    self.e.db.execute('UPDATE outbox SET state=?,attempts=?,available_at=?,last_error=? WHERE id=?',
                        ('failed' if permanent else 'pending', attempts, now + delay, str(exc), row['id']))
                    if permanent and row['method'] not in ('answerCallbackQuery',) and row['job_id']:
                        self.e.say(self.e.editors_chat, f'{self.e.admins()}\nDelivery failed for VID-{row["job_id"]:04d} (Telegram code {exc.code}). {html.escape(exc.reason)}. After correcting it, use /retry.', f'failure:{row["id"]}')
                log.warning('Outgoing item %s (%s, job %s) failed with API code %s: %s', row['id'], row['method'], row['job_id'], exc.code, exc.reason)
                if chat:
                    blocked_chats.add(chat)
                    self.chat_next[chat] = now + delay

    def deliver(self, row, payload):
        e = self.e
        method = row['method']
        guard = payload.pop('_guard', None)
        if guard and not e.alert_valid(e.job(row['job_id']), guard):
            with e.db:
                e.db.execute("UPDATE outbox SET state='skipped' WHERE id=?", (row['id'],))
            return
        if method == 'assignment':
            job = e.job(row['job_id'])
            if job['status'] != 'dispatching':
                with e.db:
                    e.db.execute("UPDATE outbox SET state='skipped' WHERE id=?", (row['id'],))
                return
            now = e.clock()
            params, due = e.assignment_payload(job, now)
            if job['file_key'] and job['file_key'].startswith(('http://', 'https://')):
                result = self.api.call('sendMessage', chat_id=params['chat_id'], text=params['caption'] + '\n\nSource: ' + html.escape(job['file_key']),
                                       parse_mode='HTML', reply_markup=params['reply_markup'])
            else:
                result = self.api.call('copyMessage', **params)
            with e.db:
                e.assignment_delivered(job['id'], result['message_id'], now, due)
                e.db.execute("UPDATE outbox SET state='sent' WHERE id=?", (row['id'],))
        elif method == 'report':
            start, end = payload['start'], payload['end']
            rows = e.report_rows(start, end)
            title = f'{datetime.fromtimestamp(start, e.c.tz):%d %b} – {datetime.fromtimestamp(end, e.c.tz) - timedelta(days=1):%d %b %Y} | {e.c.timezone}'
            images, csv_path = build_report(rows, title, 'reports', f'weekly-{int(start)}-{row["id"]}')
            with e.db:
                e.say(payload['chat_id'], '<b>Weekly editor report</b>\n' + html.escape(title + '\n' + recognition(rows)))
                for path in images:
                    e.s.enqueue('sendPhoto', {'chat_id': payload['chat_id'], '_file': str(path.resolve()),
                        '_report': {'rows': rows, 'title': title, 'page': images.index(path)}})
                e.s.enqueue('sendDocument', {'chat_id': payload['chat_id'], '_file': str(csv_path.resolve()), 'caption': 'Weekly performance data',
                    '_report': {'rows': rows, 'title': title}})
                e.db.execute("UPDATE outbox SET state='sent' WHERE id=?", (row['id'],))
        else:
            snapshot = payload.pop('_report', None)
            if snapshot and not Path(payload['_file']).exists():
                # A Render restart deletes local reports. Rebuild the saved snapshot.
                images, csv_path = build_report(snapshot['rows'], snapshot['title'], 'reports', f'recovered-{row["id"]}')
                payload['_file'] = str((images[snapshot['page']] if method == 'sendPhoto' else csv_path).resolve())
            self.api.call(method, **payload)
            with e.db:
                e.db.execute("UPDATE outbox SET state='sent' WHERE id=?", (row['id'],))

    def run(self, stop=None, health=None):
        import threading
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                if not self.e.s.lease():
                    if health:
                        health.beat()
                    stop.wait(5)
                    continue
                pending = self.e.db.execute("SELECT 1 FROM outbox WHERE state='pending' LIMIT 1").fetchone()
                updates = self.api.call('getUpdates', offset=self.e.s.get('offset', 0), timeout=2 if pending else 20,
                                        allowed_updates=['message', 'callback_query'])
                for update in updates:
                    self.e.handle(update)
                self.e.tick()
                self.flush()
                if health:
                    health.beat()
            except TelegramError as exc:
                log.warning('Telegram unavailable (code %s). Will retry.', exc.code)
                if exc.code in (401, 409):
                    raise SystemExit('Invalid token or another bot process is polling. Correct this before restarting.') from None
                stop.wait(max(3, min(exc.retry_after, 60)))
