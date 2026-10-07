import json
import os
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from video_bot.config import Config
from video_bot.dashboard import Dashboard, issue_login
from video_bot.engine import Engine
from video_bot.health import Health
from video_bot.store import Store


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.pg_cleanup = None
        url = os.getenv('TEST_DATABASE_URL', '')
        if url:
            import psycopg
            from psycopg import sql
            from psycopg.conninfo import make_conninfo
            schema = 'dashboard_' + uuid.uuid4().hex
            admin = psycopg.connect(url, autocommit=True)
            admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            self.pg_cleanup = (admin, schema)
            url = make_conninfo(url, options=f'-c search_path={schema}')
        self.config = Config(admins=(99,), editors=-100123, uploaders=-100456,
                             database=str(Path(self.folder.name) / 'bot.db'), database_url=url, dashboard_url='http://127.0.0.1')
        self.store = Store(self.config.database, url)
        self.e = Engine(self.config, self.store)
        with self.store.db:
            self.store.set('bot_username', 'test_video_bot')
            for uid in (1, 2):
                self.store.db.execute('INSERT INTO editors(id,name) VALUES (?,?)', (uid, f'Editor {uid}'))
                self.store.db.execute('''INSERT INTO jobs(source_chat,source_message,brief,effort,created,status,editor_id,assigned,due,card_id)
                    VALUES (-100456,?,?,2,?,'assigned',?,?,?,?)''', (uid, f'Video {uid}', time.time(), uid, time.time(), time.time()+86400, uid+100))
        self.server = Health().start(0, Dashboard(self.config))
        self.config.dashboard_url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.store.db.close()
        if self.pg_cleanup:
            from psycopg import sql
            admin, schema = self.pg_cleanup
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            admin.close()
        self.folder.cleanup()

    def request(self, path, data=None, cookie=None, headers=None):
        request_headers = {'Content-Type': 'application/json', 'X-Dashboard': '1'}
        if cookie:
            request_headers['Cookie'] = cookie
        request_headers.update(headers or {})
        request = Request(self.config.dashboard_url + path, data=json.dumps(data).encode() if data is not None else None, headers=request_headers)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, json.loads(body) if response.headers['Content-Type'] == 'application/json' else body, response.headers

    def token(self, uid=1):
        with self.store.db:
            return issue_login(self.e, uid).split('#login=')[1]

    def login(self, uid=1):
        status, _, headers = self.request('/api/login', {'token': self.token(uid)})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';')[0]

    def test_public_page_has_no_private_data_and_api_requires_login(self):
        status, body, headers = self.request('/')
        self.assertEqual(status, 200)
        self.assertNotIn(b'Video 1', body)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(self.request('/api/state')[0], 401)

    def test_one_time_login_and_cookie_protection(self):
        token = self.token()
        status, _, headers = self.request('/api/login', {'token': token})
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(self.request('/api/login', {'token': token})[0], 400)
        self.assertNotEqual(self.store.db.execute("SELECT token_hash FROM web_auth WHERE kind='session'").fetchone()[0], token)
        self.assertIn('Secure', Dashboard(Config(dashboard_url='https://example.com')).cookie('test'))

    def test_expired_login_rejected(self):
        token = self.token()
        with self.store.db:
            self.store.db.execute('UPDATE web_auth SET expires=0')
        self.assertEqual(self.request('/api/login', {'token': token})[0], 400)

    def test_editor_sees_only_own_work_admin_sees_team(self):
        editor = self.request('/api/state', cookie=self.login())[1]
        self.assertEqual([j['id'] for j in editor['jobs']], [1])
        self.assertEqual(editor['editors'], [])
        self.assertIsNone(editor['upload_url'])
        admin = self.request('/api/state', cookie=self.login(99))[1]
        self.assertEqual(len(admin['jobs']), 2)
        self.assertEqual(len(admin['editors']), 2)

    def test_action_saved_without_telegram_and_stale_retry_rejected(self):
        cookie = self.login()
        job = self.request('/api/state', cookie=cookie)[1]['jobs'][0]
        data = {'action': 'start_job', 'job_id': 1, 'version': job['version']}
        status, result, _ = self.request('/api/action', data, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(result['jobs'][0]['status'], 'editing')
        self.assertEqual(self.e.job(1)['status'], 'editing')
        self.assertEqual(self.request('/api/action', data, cookie)[0], 400)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='started'").fetchone()[0], 1)

    def test_editor_cannot_change_other_job_or_use_admin_action(self):
        cookie = self.login()
        for jid, action in ((2, 'start_job'), (1, 'cancel')):
            data = {'action': action, 'job_id': jid, 'version': self.e.ui_token(self.e.job(jid)), 'reason': 'No'}
            self.assertEqual(self.request('/api/action', data, cookie)[0], 400)
        self.assertEqual(self.e.job(1)['status'], 'assigned')
        self.assertEqual(self.request('/api/action', {'action':'availability', 'editor_id':2, 'available':False}, cookie)[0], 400)

    def test_concurrent_actions_only_apply_once(self):
        cookie = self.login(99)
        data = {'action':'extend', 'job_id':1, 'version':self.e.ui_token(self.e.job(1)), 'hours':24, 'reason':'More time'}
        due = self.e.job(1)['due']
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.request('/api/action', data, cookie)[0], range(2)))
        self.assertEqual(sorted(responses), [200, 400])
        self.assertEqual(self.e.job(1)['due'], due + 86400)

    def test_csrf_and_invalid_requests_are_rejected(self):
        cookie = self.login()
        self.assertEqual(self.request('/api/logout', {}, cookie, {'X-Dashboard':''})[0], 403)
        self.assertEqual(self.request('/api/logout', {}, cookie, {'Origin':'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/login', [1,2,3])[0], 400)
        self.assertEqual(self.request('/api/action')[0], 405)

    def test_logout_and_removed_editor_revoke_access(self):
        cookie = self.login()
        self.assertEqual(self.request('/api/logout', {}, cookie)[0], 200)
        self.assertEqual(self.request('/api/state', cookie=cookie)[0], 401)
        cookie = self.login()
        with self.store.db:
            self.e.editor_left({'id':1})
        self.assertEqual(self.request('/api/state', cookie=cookie)[0], 400)

    def test_login_link_only_issued_in_private_chat(self):
        def update(uid, chat, seq):
            self.e.handle({'update_id':seq,'message':{'message_id':seq,'from':{'id':uid},'chat':{'id':chat},'text':'/dashboard'}})
        update(1, -100123, 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM web_auth').fetchone()[0], 0)
        update(1, 1, 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM web_auth').fetchone()[0], 1)
        row = self.store.db.execute("SELECT payload FROM outbox WHERE method='sendMessage' AND payload LIKE '%#login=%'").fetchone()
        self.assertEqual(json.loads(row[0])['chat_id'], 1)

    def test_review_and_reassignment_keep_existing_bot_rules(self):
        cookie = self.login(99)
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET status='submitted',submission_chat=-100123,submission_message=400 WHERE id=1")
        job = self.e.job(1)
        status, result, _ = self.request('/api/action', {'action':'revise','job_id':1,'version':self.e.ui_token(job),'reason':'Shorter opening'}, cookie)
        self.assertEqual(status, 200)
        job = self.e.job(1)
        self.assertEqual(job['status'], 'revision')
        response_job = next(j for j in result['jobs'] if j['id'] == 1)
        self.assertEqual(response_job['feedback'], 'Shorter opening')
        self.assertEqual(self.request('/api/action', {'action':'unassign','job_id':1,'version':self.e.ui_token(job),'reason':'Team change'}, cookie)[0], 200)
        self.assertEqual(self.e.job(1)['status'], 'queued')
        self.assertEqual(self.store.get('unassigned_editor:1'), 1)
