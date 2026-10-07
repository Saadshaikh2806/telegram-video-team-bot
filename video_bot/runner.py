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
        self.groups_checked = False
        self.admins_chat_checked = None

    def reconcile_groups(self):
        # Recover upgrades whose service messages arrived before this version.
        chats = [self.e.uploaders, self.e.editors_chat]
        chats.extend(row[0] for row in self.e.db.execute("SELECT DISTINCT source_chat FROM jobs WHERE status NOT IN ('approved','cancelled')").fetchall())
        for chat in dict.fromkeys(chats):
            if not chat:
                continue
            try:
                result = self.api.call('getChat', chat_id=chat)
                new = result.get('id', chat)
            except TelegramError as exc:
                if not exc.migrate_to_chat_id:
                    if exc.code in (400, 403) and chat not in (self.e.uploaders, self.e.editors_chat):
                        log.warning('An older job source is inaccessible; current group processing will continue')
                        continue
                    raise
                new = exc.migrate_to_chat_id
            if new != chat:
                with self.e.db:
                    self.e.migrate_chat(chat, new)
                log.info('Recovered upgraded group binding; affected deliveries requeued')
        self.groups_checked = True

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
                if exc.migrate_to_chat_id and chat:
                    with self.e.db:
                        migrated = self.e.migrate_chat(chat, exc.migrate_to_chat_id)
                    if migrated:
                        log.info('Updated upgraded group binding; affected deliveries will retry')
                        break  # Reload payloads instead of sending stale queued chat IDs.
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
        if method in ('ui_menu', 'ui_card', 'ui_prompt'):
            if method == 'ui_menu':
                saved_menu = e.s.get(f'ui_menu:{e.editors_chat}')
                result = {'message_id': saved_menu} if saved_menu else self.api.call('sendMessage', **e.ui_menu_payload())
                if saved_menu:
                    self.api.call('editMessageText', message_id=saved_menu, **e.ui_menu_payload())
                with e.db:
                    e.s.set(f'ui_menu:{e.editors_chat}', result['message_id'])
                try:
                    self.api.call('pinChatMessage', chat_id=e.editors_chat, message_id=result['message_id'], disable_notification=True)
                except TelegramError as exc:
                    if exc.code not in (400, 403):
                        raise
                    log.warning('Team controls posted; a group admin can pin it manually')
            elif method == 'ui_prompt':
                key, request = payload.pop('key'), payload.pop('request')
                pending = e.s.get(key)
                if pending and pending['request'] == request:
                    result = self.api.call('sendMessage', **payload)
                    with e.db:
                        e.s.set(key, {**pending, 'message_id': result['message_id']})
            else:
                job = e.job(row['job_id'])
                params = e.ui_card(job)
                key = f'ui_card:{e.editors_chat}:{job["id"]}'
                saved = e.s.get(key)
                token = e.ui_token(job)
                if not saved or saved['token'] != token:
                    if saved:
                        self.api.call('editMessageText', message_id=saved['message_id'], **params)
                        result = saved
                    else:
                        result = self.api.call('sendMessage', **params)
                    with e.db:
                        e.s.set(key, {'message_id': result['message_id'], 'token': token})
            with e.db:
                e.db.execute("UPDATE outbox SET state='sent' WHERE id=?", (row['id'],))
        elif method == 'assignment':
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
                if not self.groups_checked:
                    self.reconcile_groups()
                # Refresh before any registrations, reservations or deliveries.
                # On API failure the loop retries without assigning from a stale list.
                if self.e.editors_chat:
                    admins = self.api.call('getChatAdministrators', chat_id=self.e.editors_chat)
                    with self.e.db:
                        self.e.set_group_admins(member['user']['id'] for member in admins)
                    self.admins_chat_checked = self.e.editors_chat
                pending = self.e.db.execute("SELECT 1 FROM outbox WHERE state='pending' LIMIT 1").fetchone()
                updates = self.api.call('getUpdates', offset=self.e.s.get('offset', 0), timeout=2 if pending else 20,
                                        allowed_updates=['message', 'callback_query', 'chat_member'])
                for update in updates:
                    self.e.handle(update)
                    if self.e.editors_chat and self.e.editors_chat != self.admins_chat_checked:
                        admins = self.api.call('getChatAdministrators', chat_id=self.e.editors_chat)
                        with self.e.db:
                            self.e.set_group_admins(member['user']['id'] for member in admins)
                        self.admins_chat_checked = self.e.editors_chat
                self.e.tick()
                self.flush()
                if health:
                    health.beat()
            except TelegramError as exc:
                log.warning('Telegram unavailable (code %s). Will retry.', exc.code)
                if exc.code in (401, 409):
                    raise SystemExit('Invalid token or another bot process is polling. Correct this before restarting.') from None
                stop.wait(max(3, min(exc.retry_after, 60)))
