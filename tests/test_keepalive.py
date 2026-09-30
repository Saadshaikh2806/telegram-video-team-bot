import threading
import unittest
from unittest.mock import MagicMock, patch

from video_bot.keepalive import health_url, ping, start_keepalive


class KeepAliveTests(unittest.TestCase):
    def test_public_origin_becomes_health_url(self):
        self.assertEqual(health_url('https://example.onrender.com/'), 'https://example.onrender.com/health')
        for bad in ('http://example.com', 'https://user:secret@example.com', 'https://example.com/?token=secret', 'https://example.com/path'):
            with self.assertRaises(ValueError):
                health_url(bad)

    def test_ping_has_timeout_and_recovers_from_network_errors(self):
        with patch('video_bot.keepalive.urlopen') as open_url:
            response = MagicMock(status=200)
            open_url.return_value.__enter__.return_value = response
            self.assertTrue(ping('https://example.onrender.com/health'))
            self.assertEqual(open_url.call_args.kwargs['timeout'], 10)
            response.read.assert_called_once_with(256)
            open_url.side_effect = TimeoutError()
            self.assertFalse(ping('https://example.onrender.com/health'))

    def test_no_ping_without_url_or_when_disabled(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(start_keepalive(threading.Event()))
        with patch.dict('os.environ', {'KEEP_ALIVE_ENABLED': 'false', 'RENDER_EXTERNAL_URL': 'https://example.onrender.com'}, clear=True):
            self.assertIsNone(start_keepalive(threading.Event()))

    def test_shutdown_stops_worker_before_first_ping(self):
        stop = threading.Event()
        stop.set()
        with patch.dict('os.environ', {'RENDER_EXTERNAL_URL': 'https://example.onrender.com'}, clear=True), patch('video_bot.keepalive.ping') as request:
            worker = start_keepalive(stop)
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            request.assert_not_called()
