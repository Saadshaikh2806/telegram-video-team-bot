"""Run a local dashboard with disposable sample data and no Telegram calls."""
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from video_bot.config import Config
from video_bot.dashboard import Dashboard, issue_login
from video_bot.engine import Engine
from video_bot.health import Health
from video_bot.store import Store


def main():
    with tempfile.TemporaryDirectory(prefix='video-dashboard-') as directory, ExitStack() as cleanup:
        config = Config(admins=(99,), editors=-100123, uploaders=-100456,
                        database=str(Path(directory) / 'demo.db'), dashboard_url='http://127.0.0.1:8766')
        store = Store(config.database)
        cleanup.callback(store.db.close)
        engine = Engine(config, store)
        now = time.time()
        with store.db:
            store.set('dashboard_demo', True)
            for uid, name in ((99, 'You'), (1, 'Maya Patel'), (2, 'Arjun Shah')):
                store.db.execute('INSERT INTO editors(id,name) VALUES (?,?)', (uid, name))
            samples = [
                ('ADCI · A story worth sharing\n60-second testimonial with English captions', 'assigned', 99, 8),
                ('Inside the studio\nA quick look behind the scenes', 'editing', 1, 18),
                ('Meet the team\nVertical edit for the October series', 'submitted', 2, 3),
                ('A new perspective\nTighten the opening and simplify the lower thirds', 'revision', 1, -2),
                ('Community voices\nCut the interview into a 30-second reel', 'queued', None, 0),
                ('The launch story\nFinal social cut, 9:16', 'approved', 2, 24),
            ]
            for i, (brief, status, editor, hours) in enumerate(samples, 1):
                store.db.execute('''INSERT INTO jobs(source_chat,source_message,brief,effort,created,status,editor_id,assigned,due,card_id,submission_chat,submission_message)
                    VALUES (-100456,?,?,2,?,?,?,?,?,?,-100123,?)''',
                    (i, brief, now, status, editor, now if editor else None, now + hours * 3600 if editor else None, i+100, i+200 if status=='submitted' else None))
            url = issue_login(engine, 99)
        health = Health()
        server = health.start(8766, Dashboard(config), host='127.0.0.1')
        print('Disposable demo only. Open this private local link:', flush=True)
        print(url, flush=True)
        try:
            while True:
                health.beat()
                time.sleep(10)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
