import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from video_bot.config import Config
from video_bot.health import Health
from video_bot.engine import Engine
from video_bot.store import Store
from video_bot.runner import Runner


class HostingTests(unittest.TestCase):
    def test_health_endpoint_tracks_worker_progress(self):
        health = Health()
        server = health.start(0)
        url = f'http://127.0.0.1:{server.server_port}/health'
        try:
            with urlopen(url) as response:
                self.assertEqual(json.load(response), {'status': 'ok'})
            health.last_progress = time.monotonic() - 181
            with self.assertRaises(HTTPError) as failure:
                urlopen(url)
            self.assertEqual(failure.exception.code, 503)
            health.beat()
            with urlopen(url) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()

    def test_render_requires_durable_storage(self):
        with patch.dict(os.environ, {'RENDER': 'true', 'DATABASE_URL': ''}, clear=True), patch('video_bot.config.load_env'):
            with self.assertRaisesRegex(ValueError, 'durable Postgres'):
                Config.from_env()

    def test_report_delivery_recovers_files_after_restart(self):
        store = Store(':memory:')
        e = Engine(Config(timezone='UTC'), store)
        calls = []

        class API:
            def call(self, method, **payload):
                self_outer.assertTrue(Path(payload['_file']).exists())
                self_outer.assertNotIn('_report', payload)
                calls.append(method)

        self_outer = self
        try:
            with tempfile.TemporaryDirectory() as directory:
                from video_bot.reports import build_report
                def render(rows, title, folder, stem):
                    return build_report(rows, title, directory, stem)
                with store.db:
                    store.enqueue('sendPhoto', {'chat_id': -123, '_file': str(Path(directory) / 'lost.png'),
                        '_report': {'rows': [], 'title': 'Recovered week', 'page': 0}})
                row = store.db.execute('SELECT * FROM outbox').fetchone()
                with patch('video_bot.runner.build_report', side_effect=render):
                    Runner(e, API()).deliver(row, json.loads(row['payload']))
                self.assertEqual(calls, ['sendPhoto'])
                self.assertEqual(store.db.execute('SELECT state FROM outbox').fetchone()[0], 'sent')
        finally:
            store.db.close()


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Requires disposable PostgreSQL test database')
class PostgresHostingTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        self.admin = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.schema = 'hosting_' + uuid.uuid4().hex
        self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.url = make_conninfo(os.environ['TEST_DATABASE_URL'], options=f'-c search_path={self.schema}')

    def tearDown(self):
        from psycopg import sql
        self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.admin.close()

    def test_only_one_worker_can_hold_the_lease(self):
        first, second = Store(':memory:', self.url), Store(':memory:', self.url)
        try:
            self.assertTrue(first.lease())
            self.assertFalse(second.lease())
            first.release_lease()
            self.assertTrue(second.lease())
            self.assertFalse(first.lease())
            with second.db:
                second.db.execute('UPDATE worker_lease SET expires=0')
            self.assertTrue(first.lease())
        finally:
            first.db.close()
            second.db.close()

    def test_migration_preserves_records_and_refuses_overwrite(self):
        from migrate_database import main
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'local.sqlite3')
            source = Store(path)
            with source.db:
                source.set('uploaders', -100123)
                source.db.execute('INSERT INTO editors(id,name) VALUES (?,?)', (123456789012, 'Editor'))
                source.db.execute("INSERT INTO jobs(source_chat,source_message,brief,effort,created) VALUES (-100123,5,'Brief',2,12345)")
                source.event(1, 123456789012, 'created', 'test', 12345)
            source.db.close()
            config = Config(database=path, database_url=self.url)
            with patch('migrate_database.Config.from_env', return_value=config):
                main()
                with self.assertRaisesRegex(SystemExit, 'not empty'):
                    main()
            dest = Store(path, self.url)
            try:
                self.assertEqual(dest.get('uploaders'), -100123)
                self.assertEqual(dest.db.execute('SELECT id FROM editors').fetchone()[0], 123456789012)
                with dest.db:
                    row = dest.db.execute("INSERT INTO jobs(source_chat,source_message,brief,effort,created) VALUES (-100123,6,'Next',1,12346) RETURNING id").fetchone()
                self.assertEqual(row[0], 2)
            finally:
                dest.db.close()
