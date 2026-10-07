import html
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict, deque

from .reports import build_report, recognition
from .telegram import TelegramError


log = logging.getLogger(__name__)


class Runner:
    def __init__(self, engine, api):
        self.e, self.api = engine, api
        self.chat_next = {}
        self.groups_checked = False
        self.admins_chat_checked = None
        self.admins_refresh_at = 0
        self.chat_retry_after = {}
        self.chat_sent = defaultdict(deque)
        self.next_tick = 0
        self.next_lease_check = 0

    def flush_callbacks(self):
        rows = self.e.db.execute("SELECT * FROM outbox WHERE method='answerCallbackQuery' AND state='pending' AND available_at<=? ORDER BY id LIMIT 100", (self.e.clock(),)).fetchall()
        for row in rows:
            try:
                self.deliver(row, json.loads(row['payload']))
            except TelegramError as exc:
                with self.e.db:
                    self.e.db.execute('UPDATE outbox SET state=?,available_at=? WHERE id=?',
                        ('skipped' if exc.code in (400, 403, 404) else 'pending', self.e.clock() + max(1, exc.retry_after), row['id']))

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
        self.flush_callbacks()
        now = self.e.clock()
        rows = self.e.db.execute("SELECT * FROM outbox WHERE state='pending' AND method!='answerCallbackQuery' AND available_at<=? ORDER BY priority,id LIMIT 100", (now,)).fetchall()
        sent = 0
        blocked_chats = set()
        for row in rows:
            if sent >= limit:
                break
            if self.e.db.execute('SELECT state FROM outbox WHERE id=?', (row['id'],)).fetchone()[0] != 'pending':
                continue
            payload = json.loads(row['payload'])
            chat = payload.get('chat_id', self.e.editors_chat if row['method'] == 'assignment' else 0)
            recent = self.chat_sent[chat]
            while recent and recent[0] <= now - 60:
                recent.popleft()
            paced_until = (recent[-1] + 1.05 if recent else 0) if row['priority'] <= 0 else self.chat_next.get(chat, 0)
            if chat and (chat in blocked_chats or self.chat_retry_after.get(chat, 0) > now or paced_until > now or len(recent) >= 20):
                blocked_chats.add(chat)
                continue
            try:
                delivered = self.deliver(row, payload)
                if delivered:
                    sent += 1
                    if chat:
                        self.chat_sent[chat].append(self.e.clock())
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
                    self.chat_retry_after[chat] = now + delay

    def deliver(self, row, payload):
        e = self.e
        method = row['method']
        delivered = True
        ui_guard = payload.pop('_ui_guard', None)
        if ui_guard:
            current = e.db.execute('SELECT * FROM jobs WHERE id=?', (ui_guard['job'],)).fetchone()
            if current is None or ui_guard['token'] != e.ui_token(current):
                with e.db:
                    e.db.execute("UPDATE outbox SET state='skipped' WHERE id=?", (row['id'],))
                return False
        guard = payload.pop('_guard', None)
        if guard and not e.alert_valid(e.job(row['job_id']), guard):
            with e.db:
                e.db.execute("UPDATE outbox SET state='skipped' WHERE id=?", (row['id'],))
            return
        if method in ('ui_menu', 'ui_card', 'ui_prompt'):
            if method == 'ui_menu':
                saved_menu = e.s.get(f'ui_menu:{e.editors_chat}')
                result = self.update_text(e.ui_menu_payload(), saved_menu)
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
                job = e.db.execute('SELECT * FROM jobs WHERE id=?', (pending['job'],)).fetchone() if pending else None
                if pending and pending['request'] == request and pending['expires'] > e.clock() and job and pending['token'] == e.ui_token(job):
                    result = self.api.call('sendMessage', **payload)
                    with e.db:
                        e.s.set(key, {**pending, 'message_id': result['message_id']})
                else:
                    delivered = False
            else:
                job = e.job(row['job_id'])
                params = e.ui_card(job)
                key = f'ui_card:{e.editors_chat}:{job["id"]}'
                saved = e.s.get(key)
                token = e.ui_render_token(job)
                if not saved or saved['token'] != token:
                    result = self.update_text(params, saved['message_id'] if saved else None)
                    with e.db:
                        e.s.set(key, {'message_id': result['message_id'], 'token': token})
                else:
                    delivered = False
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
            delivered = False
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

        return delivered

    def update_text(self, params, message_id=None):
        if message_id:
            try:
                self.api.call('editMessageText', message_id=message_id, **params)
                return {'message_id': message_id}
            except TelegramError as exc:
                if exc.message_not_modified:
                    return {'message_id': message_id}
                if not exc.message_to_edit_missing:
                    raise
        return self.api.call('sendMessage', **params)

    def refresh_admins(self, force=True):
        if not force and self.admins_chat_checked == self.e.editors_chat and self.e.clock() < self.admins_refresh_at:
            return
        if self.e.editors_chat:
            admins = self.api.call('getChatAdministrators', chat_id=self.e.editors_chat)
            with self.e.db:
                self.e.set_group_admins((member['user']['id'] for member in admins), authoritative=True)
            self.admins_chat_checked = self.e.editors_chat
            self.admins_refresh_at = self.e.clock() + 60

    def run(self, stop=None, health=None):
        import threading
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                if self.e.clock() >= self.next_lease_check:
                    if not self.e.s.lease():
                        if health:
                            health.beat()
                        stop.wait(5)
                        continue
                    self.next_lease_check = self.e.clock() + 20
                if not self.groups_checked:
                    self.reconcile_groups()
                # Refresh before any registrations, reservations or deliveries.
                # On API failure the loop retries without assigning from a stale list.
                self.refresh_admins(force=False)
                pending = self.e.db.execute("SELECT 1 FROM outbox WHERE state='pending' LIMIT 1").fetchone()
                updates = self.api.call('getUpdates', offset=self.e.s.get('offset', 0), timeout=0 if pending else 20,
                                        allowed_updates=['message', 'callback_query', 'chat_member'])
                for update in updates:
                    self.e.handle(update)
                    self.flush_callbacks()
                    if self.e.editors_chat and self.e.editors_chat != self.admins_chat_checked:
                        self.refresh_admins()
                if any('chat_member' in update for update in updates):
                    # Historical events must not determine roles at delivery time.
                    self.refresh_admins()
                if self.e.clock() >= self.next_tick:
                    self.e.tick()
                    self.next_tick = self.e.clock() + 1
                self.flush()
                if health:
                    health.beat()
                if pending:
                    stop.wait(0.15)
            except TelegramError as exc:
                log.warning('Telegram unavailable (code %s). Will retry.', exc.code)
                if exc.code in (401, 409):
                    raise SystemExit('Invalid token or another bot process is polling. Correct this before restarting.') from None
                stop.wait(max(3, min(exc.retry_after, 60)))
