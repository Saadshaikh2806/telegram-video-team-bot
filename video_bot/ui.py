"""Button menus and durable, user-scoped guided replies."""
import hashlib
import html
import json


def button(text, data):
    return {'text': text, 'callback_data': 'ui:' + data}


class ButtonUI:
    def ui_token(self, job):
        values = [job[k] for k in ('editor_id', 'assigned', 'status', 'submitted', 'due', 'revisions')]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:12]

    def ui_menu_payload(self):
        notice = f'\n\n<b>Solo test mode</b>: new assignments go only to {self.test_editor_id}.' if self.test_editor_id else ''
        return {'chat_id': self.editors_chat, 'text': '<b>Team controls</b>\nEditors: open My tasks, start work, then reply with your finished edit.\nAdmins: use the review and team controls below.' + notice, 'parse_mode': 'HTML',
                'reply_markup': {'inline_keyboard': [
                    [button('My tasks', 'tasks:0'), button('Waiting for review', 'reviews:0')],
                    [button('All open videos', 'jobs:0'), button('Editor availability', 'people:0')],
                    [button('Weekly report', 'report:0')]] +
                    ([[button('End solo testing', 'endtest:0')]] if self.test_editor_id else [])}}

    def ui_ensure_menu(self):
        if self.editors_chat and not self.s.get(f'ui_menu:{self.editors_chat}'):
            self.s.enqueue('ui_menu', {'chat_id': self.editors_chat}, f'ui_menu:{self.editors_chat}')
        elif self.editors_chat and self.s.get(f'ui_menu_test_mode:{self.editors_chat}', 0) != self.test_editor_id:
            self.s.enqueue('ui_menu', {'chat_id': self.editors_chat})
        if self.editors_chat:
            self.s.set(f'ui_menu_test_mode:{self.editors_chat}', self.test_editor_id)

    def ui_card(self, job):
        jid, state = job['id'], job['status']
        token = self.ui_token(job)
        def b(text, action):
            return button(text, f'{action}:{jid}:{token}')
        rows = []
        if state in ('assigned', 'editing', 'revision'):
            rows = [[b('Start editing', 'start'), b('Submit edit', 'submit')], [b('Need help', 'block')]]
        if state == 'submitted':
            rows = [[b('Approve', 'approve'), b('Request changes', 'revise')]]
        if state not in ('approved', 'cancelled'):
            admin = []
            if job['editor_id']:
                admin.append(b('Change editor', 'unassign'))
            if state in ('assigned', 'editing', 'revision'):
                admin.append(b('More time', 'extend'))
            if admin:
                rows.append(admin)
            rows.append([b('Cancel video', 'cancel')])
        names = {'dispatching': 'Sending assignment', 'assigned': 'Ready to start', 'editing': 'Editing',
                 'submitted': 'Waiting for review', 'revision': 'Changes requested', 'approved': 'Complete',
                 'queued': 'Waiting for an editor', 'cancelled': 'Cancelled'}
        text = f'<b>VID-{jid:04d} | {names[state]}</b>\nEditor: {html.escape(self.editor_name(job["editor_id"])) if job["editor_id"] else "Not assigned"}\n'
        text += 'Due: ' + (self.stamp(job['due']) if job['due'] else 'Starts when assignment is delivered')
        text += '\n\n' + html.escape(job['brief'][:1400])
        if state in ('assigned', 'editing', 'revision'):
            text += '\n\nNext: edit, then tap Submit edit or reply to the original assignment with your file.'
        elif state == 'submitted':
            text += '\n\nNext: an admin reviews the submitted edit.'
            link = job['submission_link']
            if not link and str(job['submission_chat']).startswith('-100'):
                link = f'https://t.me/c/{str(job["submission_chat"])[4:]}/{job["submission_message"]}'
            if link:
                text += f'\n<a href="{html.escape(link, quote=True)}">Open submitted edit</a>'
        return {'chat_id': self.editors_chat, 'text': text, 'parse_mode': 'HTML', 'reply_markup': {'inline_keyboard': rows}}

    def ui_refresh(self):
        if not self.editors_chat:
            return
        self.ui_ensure_menu()
        for job in self.db.execute("SELECT * FROM jobs WHERE status NOT IN ('approved','cancelled') OR id IN (SELECT job_id FROM outbox WHERE method='ui_card')").fetchall():
            key = f'ui_card_token:{self.editors_chat}:{job["id"]}'
            token = self.ui_token(job)
            if self.s.get(key) != token:
                self.s.enqueue('ui_card', {'chat_id': self.editors_chat}, job=job['id'])
                self.s.set(key, token)

    def ui_callback(self, cb):
        from .engine import UserError
        parts = cb['data'].split(':')
        action, number = parts[1], int(parts[2])
        uid = cb['from']['id']
        chat = cb['message']['chat']['id']
        if chat != self.editors_chat:
            raise UserError('Open Team controls in the Editors group.')
        if action in ('tasks', 'reviews', 'jobs', 'people', 'report', 'availability', 'endtest', 'confirmendtest'):
            if action != 'tasks':
                self.admin_only(uid)
            if action == 'endtest':
                self.say(chat, 'Resume assignments to the regular editor team? Your current assigned jobs remain recorded.',
                         reply_markup={'inline_keyboard': [[button('Yes, resume team assignments', 'confirmendtest:0')]]})
            elif action == 'confirmendtest':
                self.s.set('solo_test_disabled', True)
                self.exclude_admins()
                self.s.event(None, uid, 'solo_test_ended', 'Normal team assignments restored', self.clock())
                self.say(chat, 'Solo testing ended. New assignments now use the regular editor team.')
            elif action == 'report':
                self.command('/report', [], cb['message'], uid)
            elif action == 'availability':
                self.command('/availability', [str(number), parts[3]], cb['message'], uid)
            elif action == 'people':
                rows = [r for r in self.db.execute('SELECT * FROM editors ORDER BY name').fetchall() if r['id'] not in self.excluded_admin_ids()]
                page = rows[number:number + 10]
                buttons = [[button(('Pause ' if r['available'] else 'Resume ') + r['name'][:35], f'availability:{r["id"]}:{"off" if r["available"] else "on"}')] for r in page]
                if number + 10 < len(rows):
                    buttons.append([button('Next editors', f'people:{number+10}')])
                self.say(chat, 'Editor availability\nPause stops new assignments; current deadlines continue.' if page else 'No editors registered yet.', reply_markup={'inline_keyboard': buttons})
            else:
                where, args = "status NOT IN ('approved','cancelled')", []
                if action == 'tasks':
                    where += ' AND editor_id=?'
                    args.append(uid)
                elif action == 'reviews':
                    where = "status='submitted'"
                rows = self.db.execute(f'SELECT * FROM jobs WHERE {where} ORDER BY id LIMIT 6 OFFSET ?', (*args, number)).fetchall()
                for job in rows[:5]:
                    self.s.enqueue('sendMessage', self.ui_card(job))
                if not rows:
                    self.say(chat, 'Nothing here right now.')
                if len(rows) > 5:
                    self.say(chat, 'More videos', reply_markup={'inline_keyboard': [[button('Next videos', f'{action}:{number+5}')]]})
            return
        job = self.job(number)
        if len(parts) != 4 or parts[3] != self.ui_token(job):
            raise UserError('This video has changed. Open its latest status card or Team controls.')
        if action in ('start', 'submit', 'block'):
            self.owner_only(job, uid)
        else:
            self.admin_only(uid)
        if action == 'start':
            self.start(job, uid)
        elif action == 'approve':
            self.approve(job, uid)
        elif action in ('submit', 'block', 'revise', 'unassign', 'extend', 'cancel'):
            questions = {'submit': 'Reply with your finished video, document, or download link.', 'block': 'What is blocking your work?',
                         'revise': 'What needs changing?', 'unassign': 'Why should a different editor take this video?',
                         'extend': 'How many extra hours? Reply with a number from 1 to 168, followed by the reason.',
                         'cancel': 'Why cancel this video? Reply with the reason to confirm cancellation.'}
            key = f'ui_prompt:{chat}:{uid}'
            pending = {'action': action, 'job': number, 'token': parts[3], 'expires': self.clock() + 3600, 'request': cb['id']}
            self.s.set(key, pending)
            self.s.enqueue('ui_prompt', {'chat_id': chat, 'key': key, 'request': cb['id'],
                'text': f'<a href="tg://user?id={uid}">{html.escape(cb["from"].get("first_name", "Team member"))}</a> · VID-{number:04d}\n{questions[action]}\nReply “Never mind” to stop.',
                'parse_mode': 'HTML', 'reply_markup': {'force_reply': True, 'selective': True}})
        else:
            raise UserError('Unknown button. Open Team controls.')

    def ui_reply(self, msg):
        from .engine import UserError
        uid, chat = msg['from']['id'], msg['chat']['id']
        key = f'ui_prompt:{chat}:{uid}'
        pending = self.s.get(key)
        if not pending or not pending.get('message_id') or msg.get('reply_to_message', {}).get('message_id') != pending['message_id']:
            return False
        text = (msg.get('text') or msg.get('caption') or '').strip()
        if text.lower() in ('never mind', 'nevermind'):
            self.s.set(key, None)
            self.say(chat, 'Action cancelled. The video is unchanged.')
            return True
        job = self.job(pending['job'])
        if self.clock() > pending['expires'] or pending['token'] != self.ui_token(job):
            raise UserError('This question has expired or the video changed. Tap the action on the latest card.')
        action = pending['action']
        if action == 'submit':
            self.submit(job, uid, msg, self.link(text))
        else:
            self.command('/' + action, [str(job['id']), *text.split()], msg, uid)
        self.s.set(key, None)
        return True
