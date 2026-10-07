"""Small, authenticated dashboard; Telegram remains responsible for all files."""
import hashlib
import json
import logging
import secrets
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

from .engine import Engine, UserError
from .store import Store

STATIC = Path(__file__).with_name('web')
SESSION_SECONDS = 7 * 86400


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def role(engine, uid):
    if uid in engine.c.admins:
        return 'admin'
    if (uid in engine.excluded_admin_ids() or engine.s.get(f'editor_left:{uid}', False) or
            not engine.db.execute('SELECT 1 FROM editors WHERE id=?', (uid,)).fetchone()):
        raise UserError('Dashboard access is limited to the current editor team and configured admins.')
    return 'editor'


def issue_login(engine, uid):
    role(engine, uid)
    url = engine.c.dashboard_url
    parsed = urlsplit(url)
    if not parsed.netloc or (parsed.scheme != 'https' and not
            (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1'))):
        raise UserError('The dashboard address is not configured yet. Set DASHBOARD_URL to its HTTPS address.')
    token = secrets.token_urlsafe(32)
    engine.db.execute("DELETE FROM web_auth WHERE expires<=? OR (user_id=? AND kind='login')", (engine.clock(), uid))
    engine.db.execute('INSERT INTO web_auth VALUES (?,?,?,?)', (digest(token), uid, 'login', engine.clock() + 600))
    # The fragment never appears in HTTP access logs or Referer headers.
    return url + '/#login=' + token


def telegram_link(chat, message=None):
    if not chat or not str(chat).startswith('-100'):
        return None
    return f'https://t.me/c/{str(chat)[4:]}/' + str(message or 1)


def state(engine, uid):
    access = role(engine, uid)
    clause, args = ('', ()) if access == 'admin' else ('WHERE j.editor_id=?', (uid,))
    rows = engine.db.execute(f'''SELECT j.*,e.name AS editor_name,
        (SELECT details FROM events WHERE job_id=j.id AND kind='revise' ORDER BY id DESC LIMIT 1) AS feedback FROM jobs j
        LEFT JOIN editors e ON e.id=j.editor_id {clause}
        ORDER BY CASE WHEN j.status IN ('approved','cancelled') THEN 1 ELSE 0 END,j.id DESC LIMIT 300''', args).fetchall()
    jobs = []
    for job in rows:
        jobs.append({key: job[key] for key in ('id', 'brief', 'status', 'editor_id', 'editor_name', 'due', 'effort', 'revisions')} | {
            'version': engine.ui_token(job),
            'feedback': job['feedback'] if job['status'] == 'revision' else None,
            'file_url': telegram_link(engine.editors_chat, job['card_id']) if job['card_id'] else
                (telegram_link(job['source_chat'], job['source_message']) if access == 'admin' else None),
            'review_url': telegram_link(job['submission_chat'], job['submission_message']) if job['submission_message'] else None,
        })
    editors = []
    if access == 'admin':
        excluded = engine.excluded_admin_ids()
        editors = [dict(r) for r in engine.db.execute('SELECT id,name,available FROM editors ORDER BY name').fetchall() if r['id'] not in excluded]
    return {'user': {'id': uid, 'name': engine.editor_name(uid), 'role': access}, 'jobs': jobs,
            'editors': editors, 'test_mode': bool(engine.test_editor_id),
            'upload_url': telegram_link(engine.uploaders) if access == 'admin' else None,
            'editors_url': telegram_link(engine.editors_chat), 'now': engine.clock(), 'limit': 300,
            'demo': engine.s.get('dashboard_demo', False)}


def action(engine, uid, data):
    access = role(engine, uid)
    name = data.get('action')
    if name == 'availability':
        if access != 'admin':
            raise UserError('Only an admin can change availability.')
        if type(data.get('available')) is not bool:
            raise UserError('Choose available or paused.')
        args = [str(int(data['editor_id'])), 'on' if data['available'] else 'off']
    else:
        if name not in ('start_job', 'approve', 'revise', 'unassign', 'extend', 'cancel', 'block'):
            raise UserError('Unknown action.')
        job = engine.job(int(data['job_id']))
        if access != 'admin' and job['editor_id'] != uid:
            raise UserError('This video is assigned to another editor.')
        if data.get('version') != engine.ui_token(job):
            raise UserError('This video changed. Refresh and try again.')
        if job['status'] == 'dispatching':
            raise UserError('The assignment is being sent. Try again once it is delivered.')
        reason = str(data.get('reason', '')).strip()[:1000]
        args = [str(job['id'])]
        if name == 'extend':
            args.append(str(int(data.get('hours', 0))))
        if name in ('revise', 'unassign', 'extend', 'cancel', 'block'):
            if not reason:
                raise UserError('Please include a reason.')
            args.append(reason)
    engine.command('/' + name, args, {'chat': {'id': engine.editors_chat}}, uid)
    # Render only the changed cards. File sends/assignments stay in the bot worker.
    engine._ui_loaded_chat = engine.editors_chat
    engine.ui_refresh()


class Dashboard:
    def __init__(self, config):
        self.config = config

    def reply(self, handler, status, data, content_type='application/json', cookie=None):
        body = json.dumps(data).encode() if content_type == 'application/json' else data
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        handler.send_header('Content-Length', str(len(body)))
        handler.send_header('Cache-Control', 'no-store')
        handler.send_header('Referrer-Policy', 'no-referrer')
        handler.send_header('X-Content-Type-Options', 'nosniff')
        handler.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            handler.send_header('Set-Cookie', cookie)
        handler.end_headers()
        handler.wfile.write(body)

    def cookie(self, token, age=SESSION_SECONDS):
        local = urlsplit(self.config.dashboard_url).hostname in ('localhost', '127.0.0.1')
        return f'video_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}' + ('' if local else '; Secure')

    def handle(self, handler):
        path = urlsplit(handler.path).path
        if handler.command == 'GET' and path in ('/', '/app.js', '/style.css'):
            filename, mime = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                              '/style.css': ('style.css', 'text/css; charset=utf-8')}[path]
            self.reply(handler, 200, (STATIC / filename).read_bytes(), mime)
            return
        if path not in ('/api/login', '/api/state', '/api/action', '/api/logout', '/api/info'):
            self.reply(handler, 404, {'error': 'Not found.'})
            return
        if (handler.command == 'GET') != (path in ('/api/state', '/api/info')):
            self.reply(handler, 405, {'error': 'Method not allowed.'})
            return
        store = None
        try:
            data = {}
            if handler.command == 'POST':
                if handler.headers.get('X-Dashboard') != '1' or handler.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    self.reply(handler, 403, {'error': 'Reload the dashboard and try again.'})
                    return
                origin = handler.headers.get('Origin')
                expected = urlsplit(self.config.dashboard_url)
                if origin and origin != f'{expected.scheme}://{expected.netloc}':
                    self.reply(handler, 403, {'error': 'Request origin is not allowed.'})
                    return
                length = int(handler.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192:
                    self.reply(handler, 413, {'error': 'Request is too large.'})
                    return
                data = json.loads(handler.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError()
            store = Store(self.config.database, self.config.database_url, initialize=False)
            engine = Engine(self.config, store, initialize=False)
            if path == '/api/info':
                self.reply(handler, 200, {'bot': store.get('bot_username', '')})
                return
            cookies = SimpleCookie()
            cookies.load(handler.headers.get('Cookie', ''))
            token = cookies['video_session'].value if 'video_session' in cookies else ''
            cookie = None
            with store.db:
                if handler.command == 'POST':
                    store.begin_write()
                if path == '/api/login':
                    login = data.get('token', '')
                    if not isinstance(login, str) or not 20 <= len(login) <= 128:
                        raise UserError('This sign-in link is invalid. Request a new one from the bot.')
                    row = store.db.execute("DELETE FROM web_auth WHERE token_hash=? AND kind='login' AND expires>? RETURNING user_id",
                                           (digest(login), engine.clock())).fetchone()
                    if not row:
                        raise UserError('This sign-in link expired or was already used. Request a new one from the bot.')
                    uid = row[0]
                    role(engine, uid)
                    token = secrets.token_urlsafe(32)
                    store.db.execute('DELETE FROM web_auth WHERE expires<=?', (engine.clock(),))
                    store.db.execute('INSERT INTO web_auth VALUES (?,?,?,?)', (digest(token), uid, 'session', engine.clock() + SESSION_SECONDS))
                    cookie = self.cookie(token)
                    result = {'ok': True}
                else:
                    row = store.db.execute("SELECT user_id FROM web_auth WHERE token_hash=? AND kind='session' AND expires>?", (digest(token), engine.clock())).fetchone()
                    if not row:
                        self.reply(handler, 401, {'error': 'Sign in through Telegram to continue.'})
                        return
                    uid = row[0]
                    if path == '/api/logout':
                        store.db.execute('DELETE FROM web_auth WHERE token_hash=?', (digest(token),))
                        cookie = self.cookie('', 0)
                        result = {'ok': True}
                    else:
                        role(engine, uid)
                        if path == '/api/action':
                            with store.prioritized(0):
                                action(engine, uid, data)
                        result = state(engine, uid)
            self.reply(handler, 200, result, cookie=cookie)
        except UserError as exc:
            self.reply(handler, 400, {'error': str(exc)})
        except (ValueError, KeyError, TypeError):
            self.reply(handler, 400, {'error': 'Invalid request. Refresh and try again.'})
        except Exception:
            # Do not log requests, cookies, database credentials, or sign-in tokens.
            logging.getLogger(__name__).error('Dashboard request failed; check database availability')
            self.reply(handler, 503, {'error': 'Dashboard is temporarily unavailable. Please try again.'})
        finally:
            if store:
                store.db.close()
