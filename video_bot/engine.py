import html
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from collections import defaultdict

from .store import Store
from .ui import ButtonUI


HELP = '''Video team bot

Uploaders: post a video with an editing brief. Add #effort1, #effort2, or #effort3 (default: 2). One upload = one job; for multi-part footage use /new https://folder-link brief #effort2.

Editors:
Non-admin group members become editors automatically. Group admins and the owner are excluded. Existing members: send a normal message once.
/whoami — your Telegram ID
/myjobs — your open work
/job 12 — job details
/start_job 12 — start editing
/submit 12 https://link — submit a finished edit
Or reply to the assignment video with your edited video/document; /submit 12 in a video caption also works.
/block 12 reason — alert admins about a blocker (clock keeps running)

Admins:
/bind_uploaders and /bind_editors — run inside the two groups
/unbind_uploaders and /unbind_editors — undo a binding before any jobs exist
/availability USER_ID on|off
/editors — roster and assigned workload
/jobs — open jobs
/approve 12
/revise 12 feedback — request revision, with a new 24-hour deadline
/extend 12 HOURS reason — extend the current deadline
/cancel 12 reason
/unassign 12 reason — return a video to the queue for another editor
/report — previous calendar week's charts and CSV
/health — queue and delivery status
/retry — retry failed outgoing messages
/clear_all_data — erase bot records and start fresh (confirmation required)
'''


def label(job_id):
    return f'VID-{job_id:04d}'


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


class UserError(Exception):
    pass


class Engine(ButtonUI):
    def __init__(self, config, store: Store, clock=time.time):
        self.c, self.s, self.clock = config, store, clock
        self.db = store.db
        self.live_admin_chat = None
        with self.db:
            if not self.s.get('solo_test_disabled', False):
                if config.test_mode_auto and len(config.admins) != 1:
                    raise ValueError('Solo testing requires one configured admin or an explicit TEST_EDITOR_ID.')
                if config.test_editor_id and config.test_editor_id not in config.admins:
                    raise ValueError('TEST_EDITOR_ID must belong to a configured admin.')
            for key, value in [('uploaders', config.uploaders), ('editors_chat', config.editors)]:
                if value and self.s.get(key) is None:
                    self.s.set(key, value)
            # Apply the owner's explicit group correction once, not on every restart.
            revision = json.dumps(config.team_groups, sort_keys=True)
            if config.team_groups and self.s.get('team_groups_revision') != revision:
                destinations = {self.s.get(key): value for key, value in config.team_groups.items()
                                if key in ('uploaders', 'editors_chat') and self.s.get(key)}
                for row in self.db.execute("SELECT * FROM outbox WHERE state IN ('pending','failed')").fetchall():
                    payload = json.loads(row['payload'])
                    if payload.get('chat_id') in destinations or row['method'] == 'assignment':
                        if payload.get('chat_id') in destinations:
                            payload['chat_id'] = destinations[payload['chat_id']]
                        self.db.execute("UPDATE outbox SET payload=?,state='pending',available_at=0,attempts=0,last_error=NULL WHERE id=?", (json.dumps(payload), row['id']))
                for key, value in config.team_groups.items():
                    if key in ('uploaders', 'editors_chat'):
                        self.s.set(key, value)
                self.s.set('team_groups_revision', revision)
                self.s.event(None, None, 'groups_corrected', revision, self.clock())
            if self.s.get('report_cursor') is None:
                self.s.set('report_cursor', self.previous_week()[1])
            if self.test_editor_id and not self.s.get(f'solo_test_initialized:{self.test_editor_id}', False):
                if not self.db.execute('SELECT 1 FROM editors WHERE id=?', (self.test_editor_id,)).fetchone():
                    self.auto_editor({'id': self.test_editor_id, 'first_name': 'Test admin'})
                availability = self.db.execute("SELECT details FROM events WHERE kind='availability' AND details IN (?,?) ORDER BY id DESC LIMIT 1",
                    (f'{self.test_editor_id}: on', f'{self.test_editor_id}: off')).fetchone()
                if not availability or availability[0].endswith(': on'):
                    self.db.execute('UPDATE editors SET available=1 WHERE id=?', (self.test_editor_id,))
                self.s.set(f'solo_test_initialized:{self.test_editor_id}', True)

    @property
    def test_editor_id(self):
        return 0 if self.s.get('solo_test_disabled', False) else self.c.test_editor_id

    @property
    def uploaders(self):
        return self.s.get('uploaders', 0)

    @property
    def editors_chat(self):
        return self.s.get('editors_chat', 0)

    def say(self, chat, text, key=None, job=None, **extra):
        if chat:
            self.s.enqueue('sendMessage', {'chat_id': chat, 'text': text,
                'parse_mode': 'HTML', **extra}, key, job)

    def admins(self):
        return ' '.join(mention(uid, f'Admin {n + 1}') for n, uid in enumerate(self.c.admins))

    def stamp(self, timestamp):
        return datetime.fromtimestamp(timestamp, self.c.tz).strftime('%d %b %Y, %I:%M %p %Z')

    def job(self, job_id):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise UserError('Job not found.')
        return row

    def editor_name(self, uid):
        row = self.db.execute('SELECT name FROM editors WHERE id=?', (uid,)).fetchone()
        return row[0] if row else str(uid)

    def admin_only(self, uid):
        if uid not in self.c.admins:
            raise UserError('Only a configured admin can do that.')

    def owner_only(self, job, uid):
        if uid != job['editor_id']:
            raise UserError('Only the assigned editor can do that.')

    def handle(self, update, defer_maintenance=False):
        interactive = bool(update.get('callback_query') or update.get('message', {}).get('reply_to_message'))
        with self.s.prioritized(0 if interactive else 10), self.db:
            self.s.begin_write()
            if self.db.execute('SELECT 1 FROM processed_updates WHERE id=?', (update['update_id'],)).fetchone():
                return
            self.db.execute('SAVEPOINT incoming')
            msg = update.get('message')
            cb = update.get('callback_query')
            callback_error = None
            if cb:
                msg = cb.get('message')
            try:
                if update.get('chat_member'):
                    self.membership(update['chat_member'])
                elif cb:
                    self.callback(cb)
                elif msg:
                    self.message(msg)
            except (UserError, ValueError, IndexError) as exc:
                self.db.execute('ROLLBACK TO incoming')
                if cb:
                    callback_error = str(exc) if isinstance(exc, UserError) else 'Invalid button. Open Team controls.'
                elif msg:
                    self.say(msg['chat']['id'], html.escape(str(exc) if isinstance(exc, UserError) else 'Invalid command. Send /help for examples.'))
            self.db.execute('RELEASE incoming')
            if cb:
                self.s.enqueue('answerCallbackQuery', {'callback_query_id': cb['id'],
                    **({'text': callback_error[:200], 'show_alert': True} if callback_error else {})})
            self.db.execute('INSERT INTO processed_updates VALUES (?)', (update['update_id'],))
            self.s.set('offset', update['update_id'] + 1)
            if not defer_maintenance:
                self.assign()
                self.ui_refresh()

    def message(self, msg):
        chat = msg['chat']['id']
        if msg.get('migrate_to_chat_id'):
            self.migrate_chat(chat, msg['migrate_to_chat_id'])
            return
        if msg.get('migrate_from_chat_id'):
            self.migrate_chat(msg['migrate_from_chat_id'], chat)
            return
        user = msg.get('from', {})
        uid = user.get('id', 0)
        text = (msg.get('text') or msg.get('caption', '')).strip()
        if chat == self.editors_chat:
            for member in msg.get('new_chat_members', []):
                self.auto_editor(member, joined=True)
            if msg.get('left_chat_member'):
                self.editor_left(msg['left_chat_member'])
            if not msg.get('new_chat_members') and not msg.get('left_chat_member') and not msg.get('sender_chat') and (not text.startswith('/') or text.startswith('/whoami')):
                self.auto_editor(user)
        if text.startswith('/whoami'):
            self.say(chat, f'Your user ID: <code>{uid}</code>\nChat ID: <code>{chat}</code>')
            return
        if user.get('is_bot') or msg.get('sender_chat'):
            return  # Anonymous admins must switch to their personal identity.
        if chat == self.editors_chat and self.ui_reply(msg):
            return
        if text.split('@')[0] in ('/help', '/start', '/menu'):
            if chat == self.editors_chat:
                self.s.enqueue('sendMessage', self.ui_menu_payload())
            else:
                self.say(chat, 'Uploaders: send one video with instructions, or a source link with instructions. Editors: use the pinned Team controls in the Editors group. For setup and optional shortcuts, use /commands.')
            return
        if text.split('@')[0] == '/commands':
            self.say(chat, HELP)
            return
        if text.startswith('/unbind_'):
            self.admin_only(uid)
            command = text.split()[0].split('@')[0]
            if command not in ('/unbind_uploaders', '/unbind_editors'):
                raise UserError('Use /unbind_uploaders or /unbind_editors.')
            key = 'uploaders' if command == '/unbind_uploaders' else 'editors_chat'
            if not self.s.get(key) or self.s.get(key) != chat:
                raise UserError('Run this command inside the group currently assigned that role.')
            if self.db.execute('SELECT 1 FROM jobs LIMIT 1').fetchone():
                raise UserError('Jobs already exist, so this setup correction cannot change the groups. Existing assignments need a planned group migration.')
            self.s.set(key, 0)
            self.s.event(None, uid, command[1:], str(chat), self.clock())
            self.say(chat, 'Group role removed. Now send /bind_editors in Video Editors and /bind_uploaders in Video Uploaders. Editor registrations are preserved.')
            return
        if text.startswith('/bind_'):
            self.admin_only(uid)
            command = text.split()[0].split('@')[0]
            if command not in ('/bind_uploaders', '/bind_editors') or msg['chat']['type'] not in ('group', 'supergroup'):
                raise UserError('Run /bind_uploaders or /bind_editors inside a group.')
            key = 'uploaders' if command == '/bind_uploaders' else 'editors_chat'
            other = self.editors_chat if key == 'uploaders' else self.uploaders
            if chat == other:
                raise UserError('Uploaders and editors must use different groups.')
            if self.s.get(key) and self.s.get(key) != chat:
                raise UserError('That role is already assigned to another group. Before any jobs exist, use /unbind_uploaders or /unbind_editors in the incorrectly connected group, then bind the correct group.')
            self.s.set(key, chat)
            self.say(chat, 'Group connected. Send /help for commands.')
            return
        if chat not in (self.uploaders, self.editors_chat):
            return
        if text.startswith('/'):
            parts = text.split()
            cmd = parts[0].split('@')[0]
            self.command(cmd, parts[1:], msg, uid)
            return
        if chat == self.uploaders and self.video(msg):
            self.create_job(msg, text)
        elif chat == self.uploaders and self.link(text):
            self.create_job(msg, text, self.link(text))
        elif chat == self.editors_chat and ('video' in msg or 'document' in msg or self.link(text)):
            reply_id = msg.get('reply_to_message', {}).get('message_id')
            job = self.db.execute('SELECT * FROM jobs WHERE card_id=?', (reply_id,)).fetchone()
            if job:
                self.submit(job, uid, msg, self.link(text))

    def migrate_chat(self, old, new):
        """Apply Telegram-confirmed migration within the caller's transaction."""
        if old == new or not isinstance(new, int) or new >= 0:
            return False
        keys = [key for key in ('uploaders', 'editors_chat') if self.s.get(key) == old]
        referenced = self.db.execute('SELECT 1 FROM jobs WHERE source_chat=? OR submission_chat=? LIMIT 1', (old, old)).fetchone()
        if not keys and not referenced:
            return False
        for key in keys:
            self.s.set(key, new)
        self.db.execute('UPDATE jobs SET source_chat=? WHERE source_chat=?', (new, old))
        self.db.execute('UPDATE jobs SET submission_chat=? WHERE submission_chat=?', (new, old))
        for row in self.db.execute("SELECT * FROM outbox WHERE state IN ('pending','failed')").fetchall():
            payload = json.loads(row['payload'])
            changed = False
            for field in ('chat_id', 'from_chat_id'):
                if payload.get(field) == old:
                    payload[field] = new
                    changed = True
            reply = payload.get('reply_parameters', {})
            if reply.get('chat_id') == old:
                reply['chat_id'] = new
                changed = True
            if changed or row['method'] == 'assignment':
                self.db.execute("UPDATE outbox SET payload=?,state='pending',available_at=0,attempts=0,last_error=NULL WHERE id=?",
                                (json.dumps(payload), row['id']))
        self.s.event(None, None, 'group_migrated', f'{old} -> {new}', self.clock())
        return True

    @staticmethod
    def link(text):
        match = re.search(r'https?://[^\s<>]+', text)
        return match.group(0) if match else None

    @staticmethod
    def video(msg):
        doc = msg.get('document', {})
        return msg.get('video') or (doc if doc.get('mime_type', '').startswith('video/') or
            doc.get('file_name', '').lower().endswith(('.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v')) else None)

    def create_job(self, msg, brief, source_link=None):
        if not self.editors_chat:
            raise UserError('An admin must bind the Editors group first.')
        if msg.get('media_group_id'):
            self.say(self.uploaders, 'Please send each job as one video, or use /new with a folder link and brief. Albums are not auto-assigned.',
                     f'album:{msg["media_group_id"]}')
            return
        media = self.video(msg) or {}
        file_key = media.get('file_unique_id') or source_link
        match = re.search(r'#effort([123])\b', brief, re.I)
        effort = int(match.group(1)) if match else 2
        row = self.db.execute('INSERT INTO jobs(source_chat,source_message,file_key,brief,effort,created) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING id',
            (msg['chat']['id'], msg['message_id'], file_key, brief[:2500] or 'No brief supplied. Ask the uploader before editing.', effort, self.clock())).fetchone()
        if row is None:
            self.say(self.uploaders, 'This video or source link already has a job. Use /jobs to check its status.')
            return
        jid = row[0]
        self.s.event(jid, msg['from']['id'], 'created', brief, self.clock())
        self.say(self.uploaders, f'{label(jid)} received · effort {effort}. Waiting for an available editor.', f'received:{jid}')

    def assign(self):
        if not self.editors_chat:
            return
        self.exclude_admins()
        if self.test_editor_id:
            # Stop undelivered reservations from reaching other editors after rollout.
            for pending in self.db.execute("SELECT * FROM jobs WHERE status='dispatching' AND editor_id!=?", (self.test_editor_id,)).fetchall():
                charge = pending['effort'] if self.c.mode == 'effort' else 1
                self.db.execute('UPDATE editors SET fair_load=CASE WHEN fair_load < ? THEN 0 ELSE fair_load-? END WHERE id=?', (charge, charge, pending['editor_id']))
                self.db.execute("UPDATE jobs SET editor_id=NULL,status='queued' WHERE id=?", (pending['id'],))
                self.s.event(pending['id'], None, 'solo_test_requeued', 'Undelivered reservation moved to the test queue', self.clock())
        for job in self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id").fetchall():
            editor = self.db.execute('''SELECT e.* FROM editors e WHERE available=1 AND e.id!=?
                AND (?=0 OR e.id=?)
                AND (SELECT COUNT(*) FROM jobs j WHERE j.editor_id=e.id AND j.status IN
                ('dispatching','assigned','editing','revision','submitted')) < ?
                ORDER BY fair_load, last_assigned, id LIMIT 1''',
                (self.s.get(f'unassigned_editor:{job["id"]}', 0), self.test_editor_id, self.test_editor_id, self.c.max_active)).fetchone()
            if not editor:
                self.say(self.editors_chat, f'{self.admins()}\n{label(job["id"])} is queued: no editor has a free slot.', f'capacity:{job["id"]}')
                continue
            charge = job['effort'] if self.c.mode == 'effort' else 1
            self.db.execute("UPDATE jobs SET editor_id=?,status='dispatching' WHERE id=?", (editor['id'], job['id']))
            self.db.execute('UPDATE editors SET fair_load=fair_load+?,last_assigned=? WHERE id=?', (charge, self.clock(), editor['id']))
            self.s.event(job['id'], None, 'reserved', f'editor={editor["id"]}; charge={charge}', self.clock())
            self.s.enqueue('assignment', {}, f'assignment:{job["id"]}', job['id'])
            self.db.execute("UPDATE outbox SET state='pending',available_at=0,attempts=0,last_error=NULL WHERE method='assignment' AND job_id=?", (job['id'],))

    def assignment_payload(self, job, now):
        due = now + self.c.deadline_hours * 3600
        caption = (f'<b>{label(job["id"])}</b> · Effort {job["effort"]}\n'
                   f'Assigned to {mention(job["editor_id"], self.editor_name(job["editor_id"]))}\n'
                   f'Due: {self.stamp(due)}\n\n{html.escape(job["brief"][:450])}\n\n'
                   'Reply here with your edited video, or submit a link with /submit ' + str(job['id']) + ' https://link')
        buttons = [[{'text': 'Start editing', 'callback_data': f'start:{job["id"]}'},
                    {'text': 'Submit edit', 'callback_data': f'submit:{job["id"]}'}],
                   [{'text': 'Report blocker', 'callback_data': f'block:{job["id"]}'}]]
        return {'chat_id': self.editors_chat, 'from_chat_id': job['source_chat'],
                'message_id': job['source_message'], 'caption': caption, 'parse_mode': 'HTML',
                'reply_markup': {'inline_keyboard': buttons}}, due

    def assignment_delivered(self, jid, card_id, now, due):
        self.db.execute("UPDATE jobs SET status='assigned',card_id=?,assigned=?,due=?,original_due=? WHERE id=? AND status='dispatching'",
                        (card_id, now, due, due, jid))
        job = self.job(jid)
        self.s.event(jid, None, 'assigned', f'due={due}', now)
        self.say(self.uploaders, f'{label(jid)} assigned to {html.escape(self.editor_name(job["editor_id"]))}.\nDue: {self.stamp(due)}', f'assigned:{jid}:{card_id}')
        self.ui_refresh()

    def callback(self, cb):
        if cb.get('data', '').startswith('clear_data:'):
            return self.confirm_clear_data(cb)
        if cb.get('data', '').startswith('ui:'):
            return self.ui_callback(cb)
        msg = cb.get('message', {})
        if msg.get('chat', {}).get('id') != self.editors_chat:
            raise UserError('Use the buttons in the Editors group.')
        action, jid = cb.get('data', '').split(':', 1)
        if action == 'add_editor':
            self.say(self.editors_chat, 'Editor approval is no longer needed. Members register automatically when they join or send a normal message here.')
            return
        job, uid = self.job(int(jid)), cb['from']['id']
        if action in ('start', 'submit', 'block'):
            if msg.get('message_id') != job['card_id']:
                raise UserError('Use the latest assignment or Team controls for this video.')
            return self.ui_callback({**cb, 'data': f'ui:{action}:{jid}:{self.ui_token(job)}'})
        if action in ('approve', 'revise'):
            raise UserError('This review button is outdated. Open Waiting for review in Team controls to review the current submission.')
        raise UserError('Unknown button. Open Team controls.')

    def command(self, cmd, args, msg, uid):
        chat = msg['chat']['id']
        if cmd == '/clear_all_data':
            self.admin_only(uid)
            if chat != self.editors_chat:
                raise UserError('Run /clear_all_data in the Editors group.')
            counts = self.reset_counts()
            token = secrets.token_hex(12)
            self.s.set(f'clear_data:{uid}', {'token': token, 'chat': chat,
                'expires': self.clock() + 600, 'counts': counts})
            self.say(chat, '<b>Start fresh?</b>\n'
                f'Delete {counts[0]} video jobs, {counts[1]} editor registrations, all job history, '
                'allocation balances and pending bot messages. This cannot be undone.\n\n'
                'Group connections and the solo-test setting stay. In solo mode, the tester is re-registered with zero workload. '
                'Existing Telegram messages, files and previously exported reports are not deleted.\n\n'
                'Only the admin who requested this can confirm, within 10 minutes.',
                reply_markup={'inline_keyboard': [[{'text': 'Yes, clear all bot data', 'callback_data': f'clear_data:{token}'},
                    {'text': 'Keep my data', 'callback_data': f'clear_data:cancel:{token}'}]]})
        elif cmd == '/join':
            if chat != self.editors_chat:
                raise UserError('Send a normal message in the Editors group to be registered automatically.')
            self.auto_editor(msg['from'])
        elif cmd == '/new':
            if chat != self.uploaders or not args or not self.link(args[0]):
                raise UserError('In Uploaders, send /new https://source-link editing brief #effort2')
            self.create_job(msg, ' '.join(args), args[0])
        elif cmd == '/add_editor':
            self.say(chat, 'Manual editor registration is removed. Members register automatically when they join the Editors group or send a normal message there.')
        elif cmd == '/availability':
            self.admin_only(uid)
            eid, state = int(args[0]), args[1].lower()
            if state == 'on' and eid in self.excluded_admin_ids():
                raise UserError('Admins are excluded from editor assignments.')
            if state not in ('on', 'off') or not self.db.execute('SELECT 1 FROM editors WHERE id=?', (eid,)).fetchone():
                raise UserError('Use /availability USER_ID on or off for a registered editor.')
            baseline = self.db.execute('SELECT COALESCE(MIN(fair_load),0) FROM editors WHERE available=1 AND id!=?', (eid,)).fetchone()[0]
            balance = baseline if state == 'on' else 0
            self.db.execute('UPDATE editors SET available=?,fair_load=CASE WHEN fair_load < ? THEN ? ELSE fair_load END WHERE id=?', (state == 'on', balance, balance, eid))
            self.s.event(None, uid, 'availability', f'{eid}: {state}', self.clock())
            self.say(chat, f'Editor {eid}: {state}. Existing jobs keep their deadlines.')
        elif cmd in ('/myjobs', '/jobs'):
            if cmd == '/jobs':
                self.admin_only(uid)
            where, params = ("status NOT IN ('approved','cancelled')", ())
            if cmd == '/myjobs':
                where += ' AND editor_id=?'
                params = (uid,)
            rows = self.db.execute('SELECT * FROM jobs WHERE ' + where + ' ORDER BY id DESC LIMIT 30', params).fetchall()
            self.say(chat, '\n'.join(f'{label(j["id"])} · {j["status"]} · {self.stamp(j["due"]) if j["due"] else "awaiting assignment"}' for j in rows) or 'No open jobs. (Lists show the latest 30.)')
        elif cmd == '/editors':
            self.admin_only(uid)
            rows = [r for r in self.db.execute('SELECT * FROM editors ORDER BY id').fetchall()
                    if r['id'] not in self.excluded_admin_ids()]
            if not rows:
                self.say(chat, 'No editors detected yet. Existing members should send a normal message here once.')
            for offset in range(0, len(rows), 20):
                self.say(chat, '\n'.join(f'{html.escape(r["name"])} · ID {r["id"]} · {"available" if r["available"] else "away"} · allocation balance {r["fair_load"]}' for r in rows[offset:offset+20]))
        elif cmd == '/report':
            self.admin_only(uid)
            start, end = self.previous_week()
            self.s.enqueue('report', {'start': start, 'end': end, 'chat_id': chat})
        elif cmd == '/health':
            self.admin_only(uid)
            counts = dict(self.db.execute('SELECT state,COUNT(*) FROM outbox GROUP BY state').fetchall())
            queued = self.db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
            self.say(chat, f'Bot is running.\nQueued jobs: {queued}\nPending deliveries: {counts.get("pending",0)}\nFailed deliveries: {counts.get("failed",0)}\nUse /retry after correcting bot permissions or access.')
        elif cmd == '/retry':
            self.admin_only(uid)
            self.db.execute("UPDATE outbox SET state='pending',available_at=0,attempts=0 WHERE state='failed' AND method!='answerCallbackQuery'")
            self.say(chat, 'Failed deliveries have been queued for another attempt.')
        elif cmd in ('/job', '/start_job', '/submit', '/block', '/approve', '/revise', '/extend', '/cancel', '/unassign'):
            job = self.job(int(args[0].upper().replace('VID-', '')))
            if cmd == '/job':
                self.say(chat, f'<b>{label(job["id"])}</b> · {job["status"]}\nEditor: {html.escape(self.editor_name(job["editor_id"]))}\nDue: {self.stamp(job["due"]) if job["due"] else "pending"}\n\n{html.escape(job["brief"])}')
            elif chat != self.editors_chat:
                raise UserError('Manage and submit jobs in the Editors group.')
            elif cmd == '/start_job':
                self.start(job, uid)
            elif cmd == '/submit':
                self.submit(job, uid, msg, self.link(' '.join(args[1:])))
            elif cmd == '/approve':
                self.approve(job, uid)
            elif cmd == '/block':
                self.owner_only(job, uid)
                if job['status'] not in ('assigned', 'editing', 'revision'):
                    raise UserError('This job is not awaiting an edit.')
                reason = ' '.join(args[1:])[:1000]
                if not reason:
                    raise UserError('Include the blocker reason.')
                self.s.event(job['id'], uid, 'blocked', reason, self.clock())
                self.say(chat, f'{self.admins()}\n{label(job["id"])} is blocked: {html.escape(reason)}\nDeadline remains unchanged until an admin extends it.')
            else:
                self.admin_only(uid)
                self.change_job(cmd, job, args[1:], uid)
        else:
            raise UserError('Unknown command. Send /help for commands.')

    def reset_counts(self):
        return [self.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                for table in ('jobs', 'editors', 'events')]

    def confirm_clear_data(self, cb):
        uid = cb['from']['id']
        self.admin_only(uid)
        pending = self.s.get(f'clear_data:{uid}')
        chat = cb.get('message', {}).get('chat', {}).get('id')
        token = cb['data'].split(':')[-1]
        if not pending or token != pending['token'] or chat != pending['chat'] or chat != self.editors_chat or self.clock() > pending['expires']:
            raise UserError('This reset request expired or belongs to another admin. Send /clear_all_data again.')
        if cb['data'].startswith('clear_data:cancel:'):
            self.s.set(f'clear_data:{uid}', None)
            self.say(chat, 'Reset cancelled. Your data is unchanged.')
            return
        if self.reset_counts() != pending['counts']:
            raise UserError('The team data changed since this request. Send /clear_all_data again to review the updated totals.')
        # Keep setup and replay protection. Never reuse job IDs: old Telegram
        # buttons must not target a new job after a reset.
        keep = ('uploaders', 'editors_chat', 'team_groups_revision', 'offset', 'solo_test_disabled',
                f'group_admins:{chat}', f'ui_menu:{chat}', f'ui_menu_test_mode:{chat}')
        for table in ('outbox', 'events', 'jobs', 'editors'):
            self.db.execute(f'DELETE FROM {table}')
        self.db.execute('DELETE FROM settings WHERE key NOT IN (' + ','.join('?' for _ in keep) + ')', keep)
        self.s.set('report_cursor', self.previous_week()[1])
        if self.test_editor_id:
            self.db.execute('INSERT INTO editors(id,name) VALUES (?,?)', (self.test_editor_id, 'Test admin'))
            self.s.set(f'solo_test_initialized:{self.test_editor_id}', True)
        self.say(chat, 'Bot data cleared. You can upload a new test video now.' if self.test_editor_id else
                 'Bot data cleared. Editors should send Hi once to register, then upload a new video.')

    def membership(self, update):
        if update['chat']['id'] != self.editors_chat:
            return
        member = update['new_chat_member']
        status = member['status']
        if self.live_admin_chat != self.editors_chat:
            admins = set(self.s.get(f'group_admins:{self.editors_chat}', []))
            if status in ('administrator', 'creator'):
                admins.add(member['user']['id'])
            else:
                admins.discard(member['user']['id'])
            self.set_group_admins(admins)
        if status in ('member', 'administrator', 'creator') or (status == 'restricted' and member.get('is_member')):
            self.auto_editor(member['user'], joined=True)
        else:
            self.editor_left(member['user'])

    def editor_left(self, user):
        if user.get('is_bot') or not user.get('id'):
            return
        self.db.execute('UPDATE editors SET available=0 WHERE id=?', (user['id'],))
        self.s.set(f'editor_left:{user["id"]}', True)
        self.s.event(None, user['id'], 'editor_left', 'New assignments disabled; existing jobs retained', self.clock())

    def auto_editor(self, user, joined=False):
        if user.get('is_bot') or not user.get('id') or user['id'] in self.excluded_admin_ids():
            return
        existing = self.db.execute('SELECT 1 FROM editors WHERE id=?', (user['id'],)).fetchone()
        name = ' '.join(filter(None, [user.get('first_name'), user.get('last_name')]))[:100] or str(user['id'])
        baseline = self.db.execute('SELECT COALESCE(MIN(fair_load),0) FROM editors WHERE available=1').fetchone()[0]
        self.db.execute('INSERT INTO editors(id,name,fair_load) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name', (user['id'], name, baseline))
        if joined and self.s.get(f'editor_left:{user["id"]}'):
            self.db.execute('UPDATE editors SET available=1,fair_load=CASE WHEN fair_load < ? THEN ? ELSE fair_load END WHERE id=?', (baseline, baseline, user['id']))
            self.s.set(f'editor_left:{user["id"]}', False)
        if not existing:
            self.s.event(None, user['id'], 'editor_added_automatically', str(user['id']), self.clock())
            self.say(self.editors_chat, f'{mention(user["id"], name)} is now an editor. Assignments will be shared fairly.')

    def excluded_admin_ids(self):
        return (set(self.c.admins) | set(self.s.get(f'group_admins:{self.editors_chat}', []))) - {self.test_editor_id}

    def set_group_admins(self, ids, authoritative=False):
        self.s.set(f'group_admins:{self.editors_chat}', sorted(ids))
        if authoritative:
            self.live_admin_chat = self.editors_chat
        self.exclude_admins()

    def exclude_admins(self):
        for uid in self.excluded_admin_ids():
            self.db.execute('UPDATE editors SET available=0 WHERE id=?', (uid,))
            # Undelivered reservations can be reassigned; keep delivered job history.
            for job in self.db.execute("SELECT * FROM jobs WHERE editor_id=? AND status='dispatching'", (uid,)).fetchall():
                charge = job['effort'] if self.c.mode == 'effort' else 1
                self.db.execute('UPDATE editors SET fair_load=CASE WHEN fair_load < ? THEN 0 ELSE fair_load-? END WHERE id=?', (charge, charge, uid))
                self.db.execute("UPDATE jobs SET editor_id=NULL,status='queued' WHERE id=?", (job['id'],))
                self.db.execute("UPDATE outbox SET state='pending',available_at=0,attempts=0,last_error=NULL WHERE method='assignment' AND job_id=?", (job['id'],))
                self.s.event(job['id'], uid, 'admin_excluded', 'Undelivered assignment returned to queue', self.clock())

    def start(self, job, uid):
        self.owner_only(job, uid)
        if job['status'] not in ('assigned', 'revision', 'editing'):
            raise UserError('This job cannot be started in its current state.')
        if job['card_id']:
            self.s.enqueue('editMessageReplyMarkup', {'chat_id': self.editors_chat, 'message_id': job['card_id'],
                'reply_markup': {'inline_keyboard': [[
                    {'text': 'Submit edit', 'callback_data': f'submit:{job["id"]}'},
                    {'text': 'Need help', 'callback_data': f'block:{job["id"]}'}]]}},
                f'started-buttons:{job["id"]}:{job["card_id"]}', job['id'])
        if job['status'] == 'editing':
            return
        self.db.execute("UPDATE jobs SET status='editing' WHERE id=?", (job['id'],))
        self.s.event(job['id'], uid, 'started', '', self.clock())

    def submit(self, job, uid, msg, link):
        self.owner_only(job, uid)
        if job['status'] not in ('assigned', 'editing', 'revision'):
            raise UserError('This job is not accepting a submission. Ask an admin if changes are needed.')
        if not ('video' in msg or 'document' in msg or link):
            raise UserError('Attach the edited video/document or provide an https:// download link.')
        now = self.clock()
        # Use Telegram's message timestamp so a delivery backlog cannot make a timely edit late.
        submitted = max(job['assigned'], min(now, msg.get('date', now)))
        self.db.execute("UPDATE jobs SET status='submitted',submitted=?,first_submitted=COALESCE(first_submitted,?),submission_chat=?,submission_message=?,submission_link=? WHERE id=?",
            (submitted, submitted, msg['chat']['id'], msg['message_id'], link, job['id']))
        self.s.event(job['id'], uid, 'submitted', link or 'Telegram attachment', now)
        if self.c.approval:
            buttons = self.ui_card(self.job(job['id']))['reply_markup']['inline_keyboard']
            self.say(self.editors_chat, f'{self.admins()}\n{label(job["id"])} submitted for review. Editor deadline alerts are stopped.',
                     reply_parameters={'message_id': msg['message_id']}, reply_markup={'inline_keyboard': buttons})
            self.say(self.uploaders, f'{label(job["id"])} submitted; awaiting admin review.')
        else:
            self.finish(self.job(job['id']), uid)

    def approve(self, job, uid):
        self.admin_only(uid)
        if job['status'] != 'submitted':
            raise UserError('Only submitted jobs can be approved.')
        self.finish(job, uid)

    def finish(self, job, uid):
        self.db.execute("UPDATE jobs SET status='approved',approved=? WHERE id=?", (self.clock(), job['id']))
        self.s.event(job['id'], uid, 'approved', '', self.clock())
        self.say(self.editors_chat, f'{label(job["id"])} approved and complete.')
        self.say(self.uploaders, f'{label(job["id"])} approved. Finished edit follows.')
        self.s.enqueue('copyMessage', {'chat_id': self.uploaders, 'from_chat_id': job['submission_chat'], 'message_id': job['submission_message']},
                       f'finished:{job["id"]}:{job["submission_message"]}', job['id'])

    def change_job(self, cmd, job, args, uid):
        now = self.clock()
        if job['status'] in ('approved', 'cancelled'):
            raise UserError('This job is already closed.')
        if cmd == '/unassign':
            reason = ' '.join(args).strip()
            if not reason or not job['editor_id'] or job['status'] == 'queued':
                raise UserError('Use /unassign JOB_ID reason on an assigned, unfinished job.')
            self.s.event(job['id'], uid, 'assignment_removed', json.dumps(dict(job)), now)
            self.s.set(f'unassigned_editor:{job["id"]}', job['editor_id'])
            if job['status'] in ('dispatching', 'assigned'):
                charge = job['effort'] if self.c.mode == 'effort' else 1
                self.db.execute('UPDATE editors SET fair_load=CASE WHEN fair_load < ? THEN 0 ELSE fair_load-? END WHERE id=?', (charge, charge, job['editor_id']))
            self.db.execute("""UPDATE jobs SET status='queued',editor_id=NULL,assigned=NULL,
                due=NULL,original_due=NULL,card_id=NULL,first_submitted=NULL,submitted=NULL,
                approved=NULL,revisions=0,submission_chat=NULL,submission_message=NULL,
                submission_link=NULL WHERE id=?""", (job['id'],))
            self.db.execute("UPDATE outbox SET state='skipped' WHERE job_id=? AND state IN ('pending','failed')", (job['id'],))
            reason += '\nReturned to the queue for a different editor. A fresh deadline starts when the new assignment is delivered.'
        elif cmd == '/cancel':
            reason = ' '.join(args)
            if not reason:
                raise UserError('Include a cancellation reason.')
            self.db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job['id'],))
            if job['editor_id'] and job['status'] in ('dispatching', 'assigned'):
                charge = job['effort'] if self.c.mode == 'effort' else 1
                self.db.execute('UPDATE editors SET fair_load=CASE WHEN fair_load < ? THEN 0 ELSE fair_load-? END WHERE id=?', (charge, charge, job['editor_id']))
        elif cmd == '/revise':
            reason = ' '.join(args)
            if job['status'] != 'submitted' or not reason:
                raise UserError('Use /revise JOB_ID feedback on a submitted job.')
            self.db.execute("UPDATE jobs SET status='revision',due=?,submitted=NULL,revisions=revisions+1 WHERE id=?", (now + self.c.deadline_hours * 3600, job['id']))
        else:
            if job['status'] not in ('assigned', 'editing', 'revision'):
                raise UserError('Only a job awaiting an edit can be extended.')
            hours, reason = int(args[0]), ' '.join(args[1:])
            if not 1 <= hours <= 168 or not reason:
                raise UserError('Use /extend JOB_ID HOURS reason (1–168 hours).')
            self.db.execute('UPDATE jobs SET due=due+? WHERE id=?', (hours * 3600, job['id']))
            if job['first_submitted'] is None:
                self.db.execute('UPDATE jobs SET original_due=due WHERE id=?', (job['id'],))
        self.s.event(job['id'], uid, cmd[1:], reason, now)
        updated = self.job(job['id'])
        self.say(self.editors_chat, f'{mention(job["editor_id"], self.editor_name(job["editor_id"])) if job["editor_id"] else ""}\n{label(job["id"])}: {cmd[1:]}\n{html.escape(reason[:1000])}' + (f'\nDue: {self.stamp(updated["due"])}' if updated['due'] and cmd != '/cancel' else ''))
        self.say(self.uploaders, f'{label(job["id"])}: {cmd[1:]}. {html.escape(reason[:1000])}')

    def previous_week(self, now=None):
        local = datetime.fromtimestamp(self.clock() if now is None else now, self.c.tz)
        monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return (monday - timedelta(days=7)).timestamp(), monday.timestamp()

    def tick(self):
        now = self.clock()
        with self.db:
            self.assign()
            self.ui_refresh()
            for job in self.db.execute("SELECT * FROM jobs WHERE status IN ('assigned','editing','revision') AND due IS NOT NULL").fetchall():
                remaining = job['due'] - now
                stage = 'overdue' if remaining <= 0 else '2h' if remaining <= 2 * 3600 else '6h' if remaining <= 6 * 3600 else None
                if not stage:
                    continue
                who = self.admins() if stage == 'overdue' else mention(job['editor_id'], self.editor_name(job['editor_id']))
                text = f'{who}\n{label(job["id"])} ' + ('has not been submitted by its deadline.' if stage == 'overdue' else 'is due soon.')
                text += f'\nEditor: {html.escape(self.editor_name(job["editor_id"]))}\nDue: {self.stamp(job["due"])}'
                self.s.enqueue('sendMessage', {'chat_id': self.editors_chat, 'text': text, 'parse_mode': 'HTML',
                    '_guard': {'due': job['due'], 'stage': stage}}, f'alert:{job["id"]}:{job["due"]}:{stage}', job['id'])
            local = datetime.fromtimestamp(now, self.c.tz)
            start, end = self.previous_week()
            report_time = datetime.fromtimestamp(end, self.c.tz).replace(hour=self.c.report_hour).timestamp()
            if self.editors_chat and now >= report_time and self.s.get('report_cursor', 0) < end:
                cursor = self.s.get('report_cursor', start)
                # Recover each missed week, using local calendar boundaries (DST-safe).
                while cursor < end:
                    week_end = (datetime.fromtimestamp(cursor, self.c.tz) + timedelta(days=7)).timestamp()
                    self.s.enqueue('report', {'chat_id': self.editors_chat, 'start': cursor, 'end': week_end}, f'weekly:{cursor}')
                    cursor = week_end
                self.s.set('report_cursor', end)

    def alert_valid(self, job, guard):
        if job['status'] not in ('assigned', 'editing', 'revision') or job['due'] != guard['due']:
            return False
        remaining = job['due'] - self.clock()
        return (guard['stage'] == 'overdue' and remaining <= 0 or
                guard['stage'] == '2h' and 0 < remaining <= 7200 or
                guard['stage'] == '6h' and 7200 < remaining <= 21600)

    def report_rows(self, start, end):
        rows = []
        jobs_by_editor = defaultdict(list)
        for job in self.db.execute("SELECT * FROM jobs WHERE editor_id IS NOT NULL AND status!='cancelled'").fetchall():
            jobs_by_editor[job['editor_id']].append(job)
        for event in self.db.execute("SELECT details,at FROM events WHERE kind='assignment_removed'").fetchall():
            previous = json.loads(event['details'])
            # Removing an unfinished assignment before its deadline ends that
            # delivery obligation; do not later count it as a missed deadline.
            if previous['first_submitted'] is None and previous['original_due'] is not None and previous['original_due'] > event['at']:
                previous['original_due'] = None
            jobs_by_editor[previous['editor_id']].append(previous)
        for editor in self.db.execute('SELECT * FROM editors ORDER BY name').fetchall():
            jobs = jobs_by_editor[editor['id']]
            assigned = [j for j in jobs if j['assigned'] is not None and start <= j['assigned'] < end]
            due = [j for j in jobs if j['original_due'] is not None and start <= j['original_due'] < end]
            done = [j for j in jobs if j['approved'] is not None and start <= j['approved'] < end]
            ontime = [j for j in due if j['first_submitted'] is not None and j['first_submitted'] <= j['original_due']]
            firstpass = sum(j['revisions'] == 0 for j in done)
            rows.append(dict(editor=editor['name'], editor_id=editor['id'], assigned=len(assigned),
                assigned_effort=sum(j['effort'] for j in assigned), approved=len(done),
                completed_effort=sum(j['effort'] for j in done), due=len(due), on_time=len(ontime),
                on_time_pct=round(len(ontime) / len(due) * 100, 1) if due else None,
                first_pass_pct=round(firstpass / len(done) * 100, 1) if done else None))
        return rows
