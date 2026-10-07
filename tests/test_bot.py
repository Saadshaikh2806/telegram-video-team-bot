import json
import os
import uuid
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from video_bot.config import Config
from video_bot.engine import Engine
from video_bot.runner import Runner
from video_bot.store import Store
from video_bot.telegram import TelegramError


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.failure = None

    def call(self, method, **params):
        if self.failure:
            raise self.failure
        self.calls.append((method, params))
        if method == 'getChatAdministrators':
            return []
        return {'message_id': 1000 + len(self.calls)}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.now = 1790748000.0
        self.config = Config(admins=(99,), uploaders=-1001, editors=-1002, timezone='UTC', max_active=100)
        self.pg_cleanup = None
        url = os.getenv('TEST_DATABASE_URL', '')
        if url:
            import psycopg
            from psycopg.conninfo import make_conninfo
            from psycopg import sql
            schema = 'test_' + uuid.uuid4().hex
            admin = psycopg.connect(url, autocommit=True)
            admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            self.pg_cleanup = (admin, schema)
            url = make_conninfo(url, options=f'-c search_path={schema}')
        self.store = Store(':memory:', url)
        self.e = Engine(self.config, self.store, lambda: self.now)
        self.db = self.store.db
        self.api = FakeTelegram()
        self.r = Runner(self.e, self.api)
        self.seq = 0

    def tearDown(self):
        self.db.close()
        if self.pg_cleanup:
            from psycopg import sql
            admin, schema = self.pg_cleanup
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            admin.close()

    def message(self, uid, text='', chat=-1002, **fields):
        self.seq += 1
        msg = dict(message_id=self.seq, date=int(self.now), chat={'id': chat, 'type': 'supergroup'},
                   text=text, **{'from': {'id': uid, 'first_name': f'Editor {uid}'}})
        msg.update(fields)
        self.e.handle({'update_id': self.seq, 'message': msg})
        return msg

    def add(self, uid):
        self.message(uid, 'Hi')

    def upload(self, effort=2, key=None):
        return self.message(50, chat=-1001, text='', caption=f'Add captions #effort{effort}',
            video={'file_unique_id': key or f'file-{self.seq}'})

    def deliver(self, jid):
        job = self.e.job(jid)
        with self.db:
            self.e.assignment_delivered(jid, jid + 1000, self.now, self.now + 86400)

    def test_equal_effort_balances_within_largest_job(self):
        for uid in (1, 2, 3):
            self.add(uid)
        for effort in [3, 1, 2, 3, 1, 1, 2, 3, 2, 1, 2, 3]:
            self.upload(effort)
        loads = [r[0] for r in self.db.execute('SELECT fair_load FROM editors')]
        self.assertLessEqual(max(loads) - min(loads), 3)
        self.assertEqual(sum(loads), 24)
        self.assertEqual(self.e.job(1)['effort'], 3)
        self.assertEqual(self.e.job(2)['effort'], 1)

    def test_rotation_equal_counts(self):
        self.config.mode = 'rotation'
        for uid in (1, 2, 3):
            self.add(uid)
        for effort in [1, 3, 2, 3, 2, 1]:
            self.upload(effort)
        counts = [r[0] for r in self.db.execute('SELECT COUNT(*) FROM jobs GROUP BY editor_id')]
        self.assertEqual(counts, [2, 2, 2])

    def test_capacity_and_leave(self):
        self.config.max_active = 1
        self.add(1)
        self.add(2)
        self.message(99, '/availability 2 off')
        self.upload()
        self.upload()
        self.assertEqual(self.e.job(2)['status'], 'queued')
        self.message(99, '/availability 2 on')
        self.assertEqual(self.e.job(2)['editor_id'], 2)

    def test_duplicate_update_and_file(self):
        self.add(1)
        msg = self.upload(key='same')
        self.e.handle({'update_id': self.seq, 'message': msg})
        self.upload(key='same')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_deadline_starts_on_delivery(self):
        self.add(1)
        self.upload()
        self.now += 90000
        self.e.tick()
        self.assertIsNone(self.e.job(1)['due'])
        row = self.db.execute("SELECT * FROM outbox WHERE method='assignment'").fetchone()
        self.r.deliver(row, {})
        self.assertEqual(self.e.job(1)['due'], self.now + 86400)
        self.assertEqual(self.api.calls[-1][0], 'copyMessage')

    def test_link_assignment_contains_brief_and_source(self):
        self.add(1)
        self.message(50, '/new https://example.org/source Add subtitles #effort3', chat=-1001)
        row = self.db.execute("SELECT * FROM outbox WHERE method='assignment'").fetchone()
        self.r.deliver(row, {})
        method, params = self.api.calls[-1]
        self.assertEqual(method, 'sendMessage')
        self.assertIn('https://example.org/source', params['text'])
        self.assertIn('VID-0001', params['text'])

    def test_24_hour_alert_once_and_no_early_escalation(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.now += 18 * 3600
        self.e.tick()
        self.e.tick()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox WHERE dedupe LIKE 'alert:%:6h'").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox WHERE dedupe LIKE '%:overdue'").fetchone()[0], 0)
        self.now += 6 * 3600
        self.e.tick()
        self.e.tick()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox WHERE dedupe LIKE '%:overdue'").fetchone()[0], 1)

    def test_submission_stops_stale_alert_and_admin_wait_not_late(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        deadline = self.e.job(1)['due']
        self.now += 86401
        self.e.tick()
        self.message(1, '/submit 1 https://example.org/edit', date=int(deadline - 10))
        row = self.db.execute("SELECT * FROM outbox WHERE dedupe LIKE '%:overdue'").fetchone()
        self.r.deliver(row, json.loads(row['payload']))
        self.assertEqual(self.db.execute('SELECT state FROM outbox WHERE id=?', (row['id'],)).fetchone()[0], 'skipped')
        self.now += 90000
        self.message(99, '/approve 1')
        rows = self.e.report_rows(deadline - 1, deadline + 1)
        self.assertEqual(rows[0]['on_time_pct'], 100)

    def test_authorization_for_submit_approve_and_availability(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.message(2, '/submit 1 https://example.org/edit')
        self.assertEqual(self.e.job(1)['status'], 'assigned')
        self.message(1, '/submit 1 https://example.org/edit')
        self.message(1, '/approve 1')
        self.assertEqual(self.e.job(1)['status'], 'submitted')
        self.message(1, '/availability 1 off')
        self.assertEqual(self.db.execute('SELECT available FROM editors WHERE id=1').fetchone()[0], 1)
        self.message(99, '/approve 1')
        self.assertEqual(self.e.job(1)['status'], 'approved')

    def test_unsubmitted_overdue_in_report_denominator(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        due = self.e.job(1)['due']
        self.now = due + 100
        rows = self.e.report_rows(due - 1, due + 1)
        self.assertEqual((rows[0]['due'], rows[0]['on_time_pct']), (1, 0))

    def test_extension_invalidates_old_alert_and_adjusts_first_deadline(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.now += 86400
        self.e.tick()
        old = self.e.job(1)['due']
        self.message(99, '/extend 1 4 waiting for footage')
        self.assertEqual(self.e.job(1)['original_due'], old + 14400)
        row = self.db.execute("SELECT * FROM outbox WHERE dedupe LIKE '%:overdue'").fetchone()
        self.r.deliver(row, json.loads(row['payload']))
        self.assertEqual(len(self.api.calls), 0)

    def test_revision_keeps_first_submission_and_tracks_quality(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.message(1, '/submit 1 https://example.org/v1')
        first = self.e.job(1)['first_submitted']
        self.now += 100
        self.message(99, '/revise 1 Fix spelling')
        self.assertEqual(self.e.job(1)['status'], 'revision')
        self.message(1, '/submit 1 https://example.org/v2')
        self.message(99, '/approve 1')
        self.assertEqual(self.e.job(1)['first_submitted'], first)
        self.assertEqual(self.e.report_rows(first - 1, self.now + 1)[0]['first_pass_pct'], 0)

    def test_auto_approval(self):
        self.config.approval = False
        self.add(1)
        self.upload()
        self.deliver(1)
        self.message(1, '/submit 1 https://example.org/edit')
        self.assertEqual(self.e.job(1)['status'], 'approved')

    def test_album_not_split_into_jobs(self):
        self.add(1)
        self.message(50, chat=-1001, video={'file_unique_id': 'x'}, media_group_id='album')
        self.message(50, chat=-1001, video={'file_unique_id': 'y'}, media_group_id='album')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox WHERE dedupe='album:album'").fetchone()[0], 1)

    def test_weekly_schedule_once_and_restart(self):
        self.now += 7 * 86400
        self.e.tick()
        count = self.db.execute("SELECT COUNT(*) FROM outbox WHERE method='report'").fetchone()[0]
        self.assertEqual(count, 1)
        self.e = Engine(self.config, self.store, lambda: self.now)
        self.e.tick()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox WHERE method='report'").fetchone()[0], count)

    def test_permanent_delivery_failure_keeps_job_without_deadline(self):
        self.add(1)
        self.upload()
        with self.db:
            self.db.execute("UPDATE outbox SET state='sent' WHERE method!='assignment'")
        self.api.failure = TelegramError(403)
        self.r.flush()
        self.assertIsNone(self.e.job(1)['due'])
        self.assertEqual(self.db.execute("SELECT state FROM outbox WHERE method='assignment'").fetchone()[0], 'failed')

    def test_persisted_offset_and_jobs_survive_reopen(self):
        if self.store.remote:
            self.skipTest('SQLite backup test; Postgres persistence uses external database')
        self.add(1)
        self.upload()
        self.deliver(1)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'test.sqlite3')
            saved = Store(path)
            self.db.backup(saved.db)
            saved.db.close()
            reopened = Store(path)
            self.assertEqual(reopened.get('report_cursor'), self.store.get('report_cursor'))
            recovered = Engine(self.config, reopened, lambda: self.now + 90000)
            recovered.tick()
            self.assertEqual(recovered.job(1)['editor_id'], 1)
            self.assertEqual(reopened.get('offset'), self.store.get('offset'))
            self.assertEqual(reopened.db.execute("SELECT COUNT(*) FROM outbox WHERE dedupe LIKE '%:overdue'").fetchone()[0], 1)
            reopened.db.close()

    def test_state_and_update_offset_rollback_together(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        before = self.e.job(1)['due']
        with patch.object(self.store, 'set', side_effect=RuntimeError('simulated disk error')):
            with self.assertRaises(RuntimeError):
                self.message(99, '/extend 1 4 waiting for footage')
        self.assertEqual(self.e.job(1)['due'], before)
        self.assertIsNone(self.db.execute('SELECT 1 FROM processed_updates WHERE id=?', (self.seq,)).fetchone())

    def test_reply_submission_and_callback_permissions(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.message(1, video={'file_unique_id': 'finished'}, reply_to_message={'message_id': 1001})
        self.assertEqual(self.e.job(1)['status'], 'submitted')
        for uid, expected in [(2, 'submitted'), (99, 'approved')]:
            self.seq += 1
            self.e.handle({'update_id': self.seq, 'callback_query': {'id': f'cb{uid}', 'from': {'id': uid},
                'data': f'ui:approve:1:{self.e.ui_token(self.e.job(1))}', 'message': {'message_id': 500, 'chat': {'id': -1002}}}})
            self.assertEqual(self.e.job(1)['status'], expected)

    def test_rate_limit_retries_without_starting_clock(self):
        self.add(1)
        self.upload()
        with self.db:
            self.db.execute("UPDATE outbox SET state='sent' WHERE method!='assignment'")
        self.api.failure = TelegramError(429, 30)
        self.r.flush()
        row = self.db.execute("SELECT * FROM outbox WHERE method='assignment'").fetchone()
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(row['available_at'], self.now + 30)
        self.assertIsNone(self.e.job(1)['due'])
        self.now += 31
        self.api.failure = None
        self.r.flush()
        self.assertEqual(self.e.job(1)['status'], 'assigned')

    def test_report_generates_png_csv_and_publication_queue(self):
        from video_bot.reports import build_report
        self.add(1)
        self.upload()
        self.deliver(1)
        rows = self.e.report_rows(self.now - 1, self.now + 86401)
        with tempfile.TemporaryDirectory() as directory:
            images, csv = build_report(rows, 'Test week', directory, 'test')
            self.assertTrue(csv.exists())
            self.assertEqual(images[0].read_bytes()[:8], b'\x89PNG\r\n\x1a\n')

    def test_cancelled_reservation_releases_capacity_and_allocation(self):
        self.config.max_active = 1
        self.add(1)
        self.upload(3)
        self.upload(1)
        self.message(99, '/cancel 1 wrong upload')
        self.assertEqual(self.e.job(1)['status'], 'cancelled')
        self.assertEqual(self.e.job(2)['status'], 'dispatching')
        self.assertEqual(self.db.execute('SELECT fair_load FROM editors WHERE id=1').fetchone()[0], 1)

    def test_members_join_without_approval_and_bots_are_excluded(self):
        self.message(99, new_chat_members=[{'id': 7, 'first_name': 'Editor'}, {'id': 8, 'is_bot': True}])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM editors').fetchone()[0], 1)
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM editors WHERE id=7').fetchone())

    def enable_solo_test(self):
        self.config.test_editor_id = 99
        self.e = Engine(self.config, self.store, lambda: self.now)
        self.r = Runner(self.e, self.api)

    def confirm_reset(self, uid=99, token=None, cancel=False):
        pending = self.store.get('clear_data:99')
        self.seq += 1
        self.e.handle({'update_id': self.seq, 'callback_query': {'id': f'reset{self.seq}',
            'from': {'id': uid}, 'data': 'clear_data:' + ('cancel:' if cancel else '') + (token or pending['token']),
            'message': {'chat': {'id': -1002}, 'message_id': 777}}})

    def test_confirmed_reset_clears_work_but_preserves_setup_and_replay_protection(self):
        self.enable_solo_test()
        original = self.upload()
        old_update = self.seq
        self.deliver(1)
        self.message(99, '/clear_all_data')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
        token = self.store.get('clear_data:99')['token']
        self.confirm_reset()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox WHERE method='assignment'").fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT fair_load FROM editors WHERE id=99').fetchone()[0], 0)
        self.assertEqual((self.e.editors_chat, self.e.uploaders, self.e.test_editor_id), (-1002, -1001, 99))
        self.e.handle({'update_id': old_update, 'message': original})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.upload()
        new_job = self.db.execute('SELECT * FROM jobs').fetchone()
        self.assertGreater(new_job['id'], 1)
        self.assertEqual(new_job['editor_id'], 99)
        self.confirm_reset(token=token)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_reset_requires_same_admin_and_can_be_cancelled(self):
        self.add(1)
        self.upload()
        self.message(1, '/clear_all_data')
        self.assertIsNone(self.store.get('clear_data:1'))
        self.message(99, '/clear_all_data')
        self.config.admins = (99, 100)
        self.confirm_reset(uid=100)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
        self.confirm_reset(cancel=True)
        self.assertIsNone(self.store.get('clear_data:99'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)

    def test_reset_rejects_expired_or_changed_preview(self):
        self.add(1)
        self.upload()
        self.message(99, '/clear_all_data')
        self.now += 601
        self.confirm_reset()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 1)
        self.message(99, '/clear_all_data')
        self.upload()
        self.confirm_reset()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2)

    def test_reset_preserves_disabled_test_mode_and_clears_regular_roster(self):
        self.add(1)
        with self.db:
            self.store.set('solo_test_disabled', True)
        self.message(99, '/clear_all_data')
        self.confirm_reset()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM editors').fetchone()[0], 0)
        self.assertTrue(self.store.get('solo_test_disabled'))

    def test_unassignable_video_does_not_block_other_videos(self):
        self.enable_solo_test()
        self.upload()
        self.deliver(1)
        self.message(99, '/unassign 1 Testing')
        self.upload()
        self.assertEqual(self.e.job(1)['status'], 'queued')
        self.assertEqual(self.e.job(2)['editor_id'], 99)

    def test_live_admin_snapshot_survives_old_member_event(self):
        self.upload()
        with patch.object(self.api, 'call', return_value=[{'user': {'id': 7}}]):
            self.r.refresh_admins()
        self.seq += 1
        self.e.handle({'update_id': self.seq, 'chat_member': {'chat': {'id': -1002},
            'new_chat_member': {'status': 'member', 'user': {'id': 7}}}})
        self.assertIn(7, self.e.excluded_admin_ids())
        self.assertEqual(self.e.job(1)['status'], 'queued')
        with patch.object(self.api, 'call', return_value=[]):
            self.r.refresh_admins()
        self.add(7)
        self.assertEqual(self.e.job(1)['editor_id'], 7)

    def test_old_review_buttons_cannot_approve_new_submission(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.message(1, '/submit 1 https://example.org/first')
        first_token = self.e.ui_token(self.e.job(1))
        self.message(99, '/revise 1 Fix captions')
        self.now += 1
        self.message(1, '/submit 1 https://example.org/second')
        for data in ('approve:1', 'revise:1', f'ui:approve:1:{first_token}'):
            self.seq += 1
            self.e.handle({'update_id': self.seq, 'callback_query': {'id': str(self.seq),
                'from': {'id': 99}, 'data': data, 'message': {'chat': {'id': -1002}, 'message_id': 555}}})
            self.assertEqual(self.e.job(1)['status'], 'submitted')
        self.tap(99, 'approve')
        self.assertEqual(self.e.job(1)['status'], 'approved')

    def test_reassignment_preserves_previous_performance_counts(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.now += 90000
        self.add(2)
        self.message(99, '/unassign 1 Missed deadline')
        self.deliver(1)
        self.message(2, '/submit 1 https://example.org/finished')
        self.message(99, '/approve 1')
        rows = {r['editor_id']: r for r in self.e.report_rows(0, self.now + 86401)}
        self.assertEqual((rows[1]['assigned'], rows[1]['due'], rows[1]['on_time']), (1, 1, 0))
        self.assertEqual((rows[2]['assigned'], rows[2]['approved'], rows[2]['on_time']), (1, 1, 1))

    def test_undelivered_unassignment_does_not_count_as_assignment(self):
        self.add(1)
        self.upload()
        self.message(99, '/unassign 1 Not delivered')
        row = self.e.report_rows(0, self.now + 86401)[0]
        self.assertEqual((row['assigned'], row['due']), (0, 0))

    def test_unassignment_before_deadline_does_not_create_false_lateness(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.message(99, '/unassign 1 Planned leave')
        self.now += 90000
        row = self.e.report_rows(0, self.now + 1)[0]
        self.assertEqual((row['assigned'], row['due']), (1, 0))

    def test_deleted_card_replaced_and_identical_edit_succeeds(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        row = self.db.execute("SELECT * FROM outbox WHERE method='ui_card' ORDER BY id DESC LIMIT 1").fetchone()
        self.r.deliver(row, json.loads(row['payload']))
        self.message(1, '/start_job 1')
        row = self.db.execute("SELECT * FROM outbox WHERE method='ui_card' ORDER BY id DESC LIMIT 1").fetchone()
        with patch.object(self.api, 'call', side_effect=[TelegramError(400, description='message to edit not found'), {'message_id': 9001}]) as api:
            self.r.deliver(row, json.loads(row['payload']))
            self.assertEqual([c.args[0] for c in api.call_args_list], ['editMessageText', 'sendMessage'])
        self.assertEqual(self.store.get('ui_card:-1002:1')['message_id'], 9001)
        with patch.object(self.api, 'call', side_effect=TelegramError(400, description='message is not modified')):
            result = self.r.update_text(self.e.ui_card(self.e.job(1)), 9001)
        self.assertEqual(result['message_id'], 9001)

    def test_deleted_menu_is_replaced_and_pinned(self):
        self.add(1)
        with self.db:
            self.store.set('ui_menu:-1002', 400)
        row = self.db.execute("SELECT * FROM outbox WHERE method='ui_menu'").fetchone()
        with patch.object(self.api, 'call', side_effect=[TelegramError(400, description='message to edit not found'), {'message_id': 9002}, True]) as api:
            self.r.deliver(row, json.loads(row['payload']))
            self.assertEqual([c.args[0] for c in api.call_args_list], ['editMessageText', 'sendMessage', 'pinChatMessage'])
        self.assertEqual(self.store.get('ui_menu:-1002'), 9002)

    def test_pause_survives_restart_and_upgrade_from_old_solo_mode(self):
        self.enable_solo_test()
        self.message(99, '/availability 99 off')
        restarted = Engine(self.config, self.store, lambda: self.now)
        self.assertEqual(restarted.db.execute('SELECT available FROM editors WHERE id=99').fetchone()[0], 0)
        with self.db:
            self.db.execute("DELETE FROM settings WHERE key='solo_test_initialized:99'")
        restarted = Engine(self.config, self.store, lambda: self.now)
        self.assertEqual(restarted.db.execute('SELECT available FROM editors WHERE id=99').fetchone()[0], 0)

    def test_disabled_solo_mode_allows_admin_configuration_changes(self):
        with self.db:
            self.store.set('solo_test_disabled', True)
        with patch.dict(os.environ, {'ADMIN_IDS': '99,100'}, clear=True), patch('video_bot.config.load_env'):
            config = Config.from_env()
        restarted = Engine(config, self.store, lambda: self.now)
        self.assertEqual(restarted.test_editor_id, 0)
        config.test_editor_id = 123  # Old environment target removed from admin list.
        restarted = Engine(config, self.store, lambda: self.now)
        self.assertEqual(restarted.test_editor_id, 0)
        with self.db:
            self.store.set('solo_test_disabled', False)
        with self.assertRaises(ValueError):
            Engine(config, self.store, lambda: self.now)

    def test_solo_test_assigns_only_admin_and_preserves_capacity(self):
        self.add(1)
        self.enable_solo_test()
        self.config.max_active = 2
        with self.db:
            self.e.set_group_admins([99])
        self.upload()
        self.upload()
        self.upload()
        self.assertEqual(self.e.job(1)['editor_id'], 99)
        self.assertEqual(self.e.job(2)['editor_id'], 99)
        self.assertEqual(self.e.job(3)['status'], 'queued')
        self.deliver(1)
        self.message(99, '/start_job 1')
        self.message(99, '/submit 1 https://example.org/test')
        self.message(99, '/approve 1')
        self.assertEqual(self.e.job(1)['status'], 'approved')
        self.assertEqual(self.e.job(3)['editor_id'], 99)

    def test_solo_test_requeues_undelivered_but_preserves_delivered_jobs(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.upload()
        self.enable_solo_test()
        self.e.tick()
        self.assertEqual(self.e.job(1)['editor_id'], 1)
        self.assertEqual(self.e.job(2)['editor_id'], 99)
        self.assertEqual(self.db.execute('SELECT fair_load FROM editors WHERE id=1').fetchone()[0], 2)

    def test_solo_unassignment_waits_and_normal_mode_can_resume(self):
        self.add(1)
        self.enable_solo_test()
        self.upload()
        self.deliver(1)
        self.message(99, '/unassign 1 Try reassignment')
        self.assertEqual(self.e.job(1)['status'], 'queued')
        with self.db:
            self.e.ui_callback({'data': 'ui:confirmendtest:0', 'from': {'id': 99},
                'message': {'chat': {'id': -1002}}})
        self.e.tick()
        self.assertEqual(self.e.job(1)['editor_id'], 1)
        self.assertIn(99, self.e.excluded_admin_ids())
        restarted = Engine(self.config, self.store, lambda: self.now)
        self.assertEqual(restarted.test_editor_id, 0)

    def tap(self, uid, action, jid=1, token=None):
        self.seq += 1
        data = f'ui:{action}:{jid}'
        if action not in ('tasks', 'reviews', 'jobs', 'people', 'report'):
            data += ':' + (token or self.e.ui_token(self.e.job(jid)))
        self.e.handle({'update_id': self.seq, 'callback_query': {'id': f'cb{self.seq}',
            'from': {'id': uid, 'first_name': 'Person'}, 'data': data,
            'message': {'chat': {'id': -1002}, 'message_id': 1001}}})

    def prompt_id(self):
        row = self.db.execute("SELECT * FROM outbox WHERE method='ui_prompt' ORDER BY id DESC LIMIT 1").fetchone()
        self.r.deliver(row, json.loads(row['payload']))
        return self.api.calls[-1][0], self.api.calls[-1][1], 1000 + len(self.api.calls)

    def test_guided_submission_review_and_revision(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.tap(1, 'submit')
        _, payload, mid = self.prompt_id()
        self.assertTrue(payload['reply_markup']['force_reply'])
        self.message(2, 'https://example.org/wrong', reply_to_message={'message_id': mid})
        self.assertEqual(self.e.job(1)['status'], 'assigned')
        self.message(1, 'https://example.org/edit', reply_to_message={'message_id': mid})
        self.assertEqual(self.e.job(1)['status'], 'submitted')
        self.assertIn('https://example.org/edit', self.e.ui_card(self.e.job(1))['text'])
        self.tap(1, 'approve')
        self.assertEqual(self.e.job(1)['status'], 'submitted')
        self.tap(99, 'revise')
        _, _, mid = self.prompt_id()
        self.message(99, 'Please fix the captions', reply_to_message={'message_id': mid})
        self.assertEqual(self.e.job(1)['status'], 'revision')

    def test_guided_unassign_and_stale_button(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        old = self.e.ui_token(self.e.job(1))
        self.tap(99, 'unassign')
        _, _, mid = self.prompt_id()
        self.message(99, 'On leave', reply_to_message={'message_id': mid})
        self.assertEqual(self.e.job(1)['status'], 'queued')
        self.tap(1, 'start', token=old)
        self.assertEqual(self.e.job(1)['status'], 'queued')

    def test_guided_extend_cancel_and_expiry(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        due = self.e.job(1)['due']
        self.tap(99, 'extend')
        _, _, mid = self.prompt_id()
        self.message(99, '4 Waiting for footage', reply_to_message={'message_id': mid})
        self.assertEqual(self.e.job(1)['due'], due + 14400)
        self.tap(99, 'cancel')
        _, _, mid = self.prompt_id()
        self.message(99, 'Never mind', reply_to_message={'message_id': mid})
        self.assertEqual(self.e.job(1)['status'], 'assigned')
        self.tap(99, 'cancel')
        _, _, mid = self.prompt_id()
        self.now += 3601
        self.message(99, 'Cancel it', reply_to_message={'message_id': mid})
        self.assertEqual(self.e.job(1)['status'], 'assigned')

    def test_status_card_updates_in_place_and_menu_pin_is_optional(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        row = self.db.execute("SELECT * FROM outbox WHERE method='ui_card' ORDER BY id DESC LIMIT 1").fetchone()
        self.r.deliver(row, json.loads(row['payload']))
        saved = self.store.get('ui_card:-1002:1')['message_id']
        self.tap(1, 'start')
        row = self.db.execute("SELECT * FROM outbox WHERE method='ui_card' ORDER BY id DESC LIMIT 1").fetchone()
        self.r.deliver(row, json.loads(row['payload']))
        self.assertEqual(self.api.calls[-1][0], 'editMessageText')
        self.assertEqual(self.api.calls[-1][1]['message_id'], saved)
        menu = self.db.execute("SELECT * FROM outbox WHERE method='ui_menu'").fetchone()
        self.r.deliver(menu, json.loads(menu['payload']))
        self.assertEqual(self.api.calls[-1][0], 'pinChatMessage')

    def test_start_removes_start_button_and_does_not_send_extra_confirmation(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.tap(1, 'start')
        card = self.e.ui_card(self.e.job(1))
        labels = [b['text'] for row in card['reply_markup']['inline_keyboard'] for b in row]
        self.assertNotIn('Start editing', labels)
        self.assertIn('Submit edit', labels)
        self.assertIn('Need help', labels)
        self.assertIn('| Editing', card['text'])
        row = self.db.execute("SELECT * FROM outbox WHERE method='editMessageReplyMarkup'").fetchone()
        payload = json.loads(row['payload'])
        self.assertEqual(payload['message_id'], 1001)
        self.assertNotIn('Start editing', json.dumps(payload))
        self.assertFalse(any('is being edited' in r[0] for r in self.db.execute("SELECT payload FROM outbox WHERE method='sendMessage'").fetchall()))
        self.message(1, '/start_job 1')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events WHERE kind='started'").fetchone()[0], 1)

    def test_start_updates_clicked_task_list_copy(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        token = self.e.ui_token(self.e.job(1))
        self.seq += 1
        self.e.handle({'update_id': self.seq, 'callback_query': {'id': 'task-copy',
            'from': {'id': 1}, 'data': f'ui:start:1:{token}',
            'message': {'chat': {'id': -1002}, 'message_id': 888}}})
        row = self.db.execute("SELECT payload FROM outbox WHERE method='editMessageText'").fetchone()
        payload = json.loads(row[0])
        self.assertEqual(payload['message_id'], 888)
        self.assertIn('| Editing', payload['text'])
        self.assertNotIn('Start editing', json.dumps(payload))

    def test_plain_source_link_creates_job(self):
        self.message(50, 'https://example.org/source Add captions', chat=-1001)
        self.assertEqual(self.e.job(1)['file_key'], 'https://example.org/source')

    def test_unassign_waits_for_different_editor_and_resets_deadline(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        old_due = self.e.job(1)['due']
        self.message(99, '/unassign VID-0001 Editor unavailable')
        job = self.e.job(1)
        self.assertEqual(job['status'], 'queued')
        for field in ('editor_id', 'due', 'card_id', 'assigned'):
            self.assertIsNone(job[field])
        self.assertEqual(self.db.execute('SELECT fair_load FROM editors WHERE id=1').fetchone()[0], 0)
        self.e.tick()
        self.assertEqual(self.e.job(1)['status'], 'queued')
        self.now += 3600
        self.add(2)
        self.assertEqual(self.e.job(1)['editor_id'], 2)
        row = self.db.execute("SELECT * FROM outbox WHERE method='assignment'").fetchone()
        self.assertEqual(row['state'], 'pending')
        self.r.deliver(row, {})
        self.assertEqual(self.e.job(1)['due'], old_due + 3600)
        self.message(1, '/submit 1 https://example.org/old-edit')
        self.assertEqual(self.e.job(1)['status'], 'assigned')

    def test_unassign_clears_submission_but_preserves_audit_and_started_effort(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.message(1, '/submit 1 https://example.org/edit')
        self.message(99, '/unassign 1 Different editor needed')
        job = self.e.job(1)
        for field in ('submitted', 'first_submitted', 'submission_chat', 'submission_message', 'submission_link'):
            self.assertIsNone(job[field])
        self.assertEqual(job['revisions'], 0)
        self.assertEqual(self.db.execute('SELECT fair_load FROM editors WHERE id=1').fetchone()[0], 2)
        audit = self.db.execute("SELECT details FROM events WHERE kind='assignment_removed'").fetchone()[0]
        self.assertEqual(json.loads(audit)['submission_link'], 'https://example.org/edit')
        self.message(99, '/approve 1')
        self.assertEqual(self.e.job(1)['status'], 'queued')

    def test_unassign_requires_admin_reason_open_assignment_and_editors_group(self):
        self.add(1)
        self.upload()
        for uid, text, chat in ((1, '/unassign 1 reason', -1002),
                                (99, '/unassign 1', -1002),
                                (99, '/unassign 1 reason', -1001)):
            self.message(uid, text, chat=chat)
            self.assertEqual(self.e.job(1)['status'], 'dispatching')
        self.message(99, '/cancel 1 Test')
        self.message(99, '/unassign 1 Test')
        self.assertEqual(self.e.job(1)['status'], 'cancelled')

    def test_unassign_skips_old_reminders_and_reassigns_immediately(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        self.add(2)
        self.now += 23 * 3600
        self.e.tick()
        self.message(99, '/unassign 1 On leave')
        self.assertEqual(self.e.job(1)['editor_id'], 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox WHERE dedupe LIKE 'alert:%' AND state='pending'").fetchone()[0], 0)

    def test_admins_cannot_register_or_receive_assignments(self):
        with self.db:
            self.e.set_group_admins([7, 8])
        for uid in (7, 8, 99):
            self.add(uid)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM editors').fetchone()[0], 0)
        self.upload()
        self.assertEqual(self.e.job(1)['status'], 'queued')
        self.add(1)
        self.assertEqual(self.e.job(1)['editor_id'], 1)

    def test_promotion_requeues_undelivered_job_and_cannot_be_reenabled(self):
        self.add(7)
        self.upload()
        self.add(1)
        self.seq += 1
        self.e.handle({'update_id': self.seq, 'chat_member': {'chat': {'id': -1002},
            'new_chat_member': {'status': 'administrator', 'user': {'id': 7}}}})
        self.assertEqual(self.e.job(1)['editor_id'], 1)
        self.assertEqual(self.db.execute('SELECT fair_load FROM editors WHERE id=7').fetchone()[0], 0)
        self.message(99, '/availability 7 on')
        self.add(7)
        self.assertEqual(self.db.execute('SELECT available FROM editors WHERE id=7').fetchone()[0], 0)

    def test_admin_sync_preserves_delivered_jobs(self):
        self.add(7)
        self.upload()
        self.deliver(1)
        with self.db:
            self.e.set_group_admins([7])
        self.assertEqual(self.e.job(1)['editor_id'], 7)
        self.assertEqual(self.e.job(1)['status'], 'assigned')
        self.upload()
        self.assertEqual(self.e.job(2)['status'], 'queued')

    def test_excluded_reservation_delivers_when_editor_later_joins(self):
        self.add(7)
        self.upload()
        with self.db:
            self.e.set_group_admins([7])
        row = self.db.execute("SELECT * FROM outbox WHERE method='assignment'").fetchone()
        self.r.deliver(row, {})
        self.add(1)
        row = self.db.execute("SELECT * FROM outbox WHERE method='assignment'").fetchone()
        self.assertEqual(row['state'], 'pending')
        self.r.deliver(row, {})
        self.assertEqual(self.e.job(1)['status'], 'assigned')
        self.assertEqual(self.e.job(1)['editor_id'], 1)

    def test_existing_member_registers_on_normal_message_only_in_editors(self):
        self.message(7, 'Hi', chat=-1001)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM editors').fetchone()[0], 0)
        self.message(7, 'Hi')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM editors').fetchone()[0], 1)
        self.message(7, 'Hi again')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM editors').fetchone()[0], 1)

    def test_leave_disables_new_assignments_and_rejoin_restores(self):
        self.add(7)
        self.message(99, left_chat_member={'id': 7})
        self.assertEqual(self.db.execute('SELECT available FROM editors WHERE id=7').fetchone()[0], 0)
        self.message(99, new_chat_members=[{'id': 7}])
        self.assertEqual(self.db.execute('SELECT available FROM editors WHERE id=7').fetchone()[0], 1)

    def test_admin_pause_is_not_undone_by_messages(self):
        self.add(7)
        self.message(99, '/availability 7 off')
        self.message(7, 'Hi')
        self.assertEqual(self.db.execute('SELECT available FROM editors WHERE id=7').fetchone()[0], 0)

    def test_membership_update_registers_editor(self):
        self.e.handle({'update_id': 1, 'chat_member': {'chat': {'id': -1002}, 'new_chat_member': {'status': 'member', 'user': {'id': 7, 'first_name': 'Editor'}}}})
        self.assertIsNotNone(self.db.execute('SELECT 1 FROM editors WHERE id=7').fetchone())

    def test_explicit_group_ids_fix_stale_settings_once(self):
        self.config.team_groups = {'editors_chat': -1004430488373, 'uploaders': -1004411321528}
        fixed = Engine(self.config, self.store, lambda: self.now)
        self.assertEqual(fixed.editors_chat, -1004430488373)
        self.assertEqual(fixed.uploaders, -1004411321528)
        with self.db:
            self.store.set('editors_chat', -100777)
        restarted = Engine(self.config, self.store, lambda: self.now)
        self.assertEqual(restarted.editors_chat, -100777)

    def test_admin_can_correct_wrong_group_binding_before_jobs(self):
        self.add(1)
        self.message(99, '/unbind_editors')
        self.message(99, '/unbind_uploaders', chat=-1001)
        self.message(99, '/bind_editors', chat=-1001)
        self.message(99, '/bind_uploaders', chat=-1002)
        self.assertEqual(self.e.editors_chat, -1001)
        self.assertEqual(self.e.uploaders, -1002)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM editors').fetchone()[0], 1)

    def test_unbind_requires_admin_and_current_group_and_no_jobs(self):
        self.message(1, '/unbind_uploaders', chat=-1001)
        self.message(99, '/unbind_uploaders', chat=-1002)
        self.assertEqual(self.e.uploaders, -1001)
        self.add(1)
        self.upload()
        self.message(99, '/unbind_uploaders', chat=-1001)
        self.assertEqual(self.e.uploaders, -1001)

    def test_unbinding_not_undone_by_restart_configuration(self):
        self.message(99, '/unbind_uploaders', chat=-1001)
        restarted = Engine(self.config, self.store, lambda: self.now)
        self.assertEqual(restarted.uploaders, 0)

    def test_group_service_migration_preserves_jobs_and_retargets_queue(self):
        self.add(1)
        self.upload()
        self.deliver(1)
        due = self.e.job(1)['due']
        self.message(1, '/submit 1 https://example.org/edit')
        with self.db:
            self.store.enqueue('sendMessage', {'chat_id': -1002, 'text': 'Test'})
            self.db.execute("UPDATE outbox SET state='failed' WHERE method='sendMessage'")
        self.message(0, migrate_to_chat_id=-100999, **{'from': {'id': 0, 'is_bot': True}})
        self.assertEqual(self.e.editors_chat, -100999)
        self.assertEqual(self.e.job(1)['submission_chat'], -100999)
        self.assertEqual(self.e.job(1)['due'], due)
        self.assertEqual(self.e.job(1)['editor_id'], 1)
        for row in self.db.execute("SELECT payload FROM outbox WHERE state='pending'").fetchall():
            self.assertNotEqual(json.loads(row[0]).get('chat_id'), -1002)

    def test_api_migration_requeues_failed_delivery_and_retries(self):
        with self.db:
            self.store.enqueue('sendMessage', {'chat_id': -1002, 'text': 'Test'})
        self.api.failure = TelegramError(400, description='upgraded to a supergroup', migrate_to_chat_id=-100999)
        self.r.flush()
        self.assertEqual(self.e.editors_chat, -100999)
        self.assertEqual(self.db.execute('SELECT state FROM outbox').fetchone()[0], 'pending')
        self.api.failure = None
        self.r.flush()
        self.assertEqual(self.api.calls[-1][1]['chat_id'], -100999)

    def test_startup_recovers_previously_missed_migration(self):
        self.add(1)
        self.upload()
        original = self.api.call
        def call(method, **payload):
            if method == 'getChat' and payload['chat_id'] == -1001:
                raise TelegramError(400, migrate_to_chat_id=-100888)
            return original(method, **payload)
        self.api.call = call
        self.r.reconcile_groups()
        self.assertEqual(self.e.uploaders, -100888)
        self.assertEqual(self.e.job(1)['source_chat'], -100888)
        self.assertTrue(self.r.groups_checked)


if __name__ == '__main__':
    unittest.main()
