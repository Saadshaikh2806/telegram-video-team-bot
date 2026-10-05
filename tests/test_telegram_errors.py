import unittest
from unittest.mock import MagicMock, patch

from video_bot.telegram import Telegram, TelegramError, safe_error_reason


class TelegramErrorTests(unittest.TestCase):
    def test_copy_failure_is_identified_without_echoing_private_text(self):
        reason = safe_error_reason('Bad Request: message to copy not found SECRET')
        self.assertIn('Original message', reason)
        self.assertNotIn('SECRET', reason)

    def test_unknown_failure_does_not_echo_description(self):
        self.assertNotIn('SECRET', safe_error_reason('unexpected SECRET'))

    def test_client_passes_rejection_reason(self):
        response = MagicMock()
        response.read.return_value = b'{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'
        with patch('video_bot.telegram.urlopen') as request:
            request.return_value.__enter__.return_value = response
            with self.assertRaises(TelegramError) as caught:
                Telegram('not-a-real-token').call('sendMessage', chat_id=-1, text='Test')
        self.assertIn('Destination group', caught.exception.reason)
