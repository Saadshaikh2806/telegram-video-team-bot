import unittest

from video_bot.database_errors import database_error_hint


class DatabaseErrorTests(unittest.TestCase):
    def test_password_failure_is_safe(self):
        hint = database_error_hint(Exception('password authentication failed; secret=DO-NOT-PRINT'))
        self.assertIn('login failed', hint)
        self.assertNotIn('DO-NOT-PRINT', hint)

    def test_direct_supabase_network_error_has_pooler_advice(self):
        hint = database_error_hint(Exception('Network is unreachable'), 'postgresql://postgres:secret@db.example.supabase.co:5432/postgres')
        self.assertIn('Session pooler', hint)
        self.assertNotIn('secret', hint)

    def test_unknown_errors_do_not_leak(self):
        self.assertNotIn('PRIVATE', database_error_hint(Exception('PRIVATE')))

    def test_placeholder_is_identified(self):
        self.assertIn('still contains', database_error_hint(Exception(), 'postgresql://postgres:[YOUR-PASSWORD]@example.com/postgres'))

    def test_wrong_pooler_username(self):
        self.assertIn('postgres.PROJECT_REFERENCE', database_error_hint(Exception('Tenant or user not found')))
