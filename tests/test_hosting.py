import json
import os
import tempfile
import time
import unittest
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
