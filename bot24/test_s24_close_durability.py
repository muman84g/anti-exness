"""No-order crash/replay coverage for the operational close transaction."""
import copy
import csv
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import live_s24_bot as s24
from test_s24_safety_regressions import params_copy, persisted_position, RecordingExecutor


class CloseDurabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [
            mock.patch.object(s24, 'STATE_FILE', str(self.root / 'state.json')),
            mock.patch.object(s24, 'TRADE_LOG_FILE', str(self.root / 'trades.csv')),
        ]
        for patch in self.patches:
            patch.start()
        self.runner = s24.S24NoAdverseRunner(params_copy())
        self.runner.state = self.runner._default_state()
        self.strategy = self.runner.params['strategies'][0]
        self.runner.executor = RecordingExecutor(positions=[], orders=[])

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def seed_core(self):
        st = self.runner._st(self.strategy)
        st['basket'] = [persisted_position()]
        st['pending_close_reason'] = 'basket_target'
        st['pending_close_signal_bar'] = '2026-01-01T13:01:00+00:00'
        self.runner._save_state()

    def seed_v206(self):
        st = self.runner.v206_lane.state
        st.update(migration_pending=False, migration_flat_confirmations=3,
                  blocked_reason=None, blocked_details={})
        st['basket'] = [{**persisted_position(), 'owner_magic': 240206,
                        'owner_comment': 's24_v206', 'fixed_stop': 2063.0,
                        'target': 2065.0, 'timeout_at_utc': '2026-01-01T13:30:00+00:00'}]
        self.runner._save_state()

    def row(self):
        return dict(event='position_close_deal', strategy_id=self.strategy['id'],
                    lane_id=1, magic=200024, symbol='XAUUSD', mt5_symbol='XAUUSD',
                    position_identifier=7001, ticket=7001, deal_id=15001,
                    timestamp_utc='2026-01-01T13:02:00+00:00', live=True,
                    profit=0.9)

    def append(self, row=None):
        return s24.append_operational_close_once(s24.TRADE_LOG_FILE, row or self.row(), s24.TRADE_FIELDS)

    def test_visible_replay_requires_file_and_directory_fsync(self):
        self.append()
        with mock.patch.object(s24.os, 'fsync') as sync, mock.patch.object(s24, '_fsync_parent_directory') as parent:
            self.assertFalse(self.append())
            self.assertGreaterEqual(sync.call_count, 1)
            parent.assert_called_once()

    def test_replay_fsync_failure_cannot_report_success(self):
        self.append()
        with mock.patch.object(s24.os, 'fsync', side_effect=OSError('sync failed')):
            with self.assertRaisesRegex(OSError, 'sync failed'):
                self.append()

    def test_replay_normalizes_optional_none_like_csv_writer(self):
        row = {**self.row(), 'signal_bar_time': None, 'note': None}
        self.assertTrue(self.append(row))
        self.assertFalse(self.append(row))

    def test_position_deal_cannot_be_reused_by_v206(self):
        self.append()
        with self.assertRaisesRegex(RuntimeError, 'conflict'):
            self.append({**self.row(), 'event': 'v206_close_confirmed', 'lane_id': 206})

    def test_noninteger_deal_cannot_be_coerced_into_identity(self):
        for value in (True, 15001.5, 15001.0):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.append({**self.row(), 'deal_id': value})

    def test_preflight_rejects_unterminated_tail(self):
        self.append()
        path = Path(s24.TRADE_LOG_FILE)
        path.write_bytes(path.read_bytes().rstrip(b'\r\n'))
        with self.assertRaisesRegex(RuntimeError, 'unterminated'):
            s24.validate_csv_schema(str(path), s24.TRADE_FIELDS)

    def test_preflight_rejects_duplicate_close_deal(self):
        self.append()
        with open(s24.TRADE_LOG_FILE, 'a', newline='', encoding='utf-8') as handle:
            csv.DictWriter(handle, s24.TRADE_FIELDS).writerow(self.row())
        with self.assertRaisesRegex(RuntimeError, 'duplicate|conflict'):
            s24.validate_csv_schema(s24.TRADE_LOG_FILE, s24.TRADE_FIELDS)

    def test_replay_rejects_same_deal_with_other_ownership(self):
        self.append()
        with self.assertRaisesRegex(RuntimeError, 'conflict'):
            self.append({**self.row(), 'lane_id': 206, 'magic': 240206})

    def test_core_derived_exception_restores_complete_state(self):
        self.seed_core()
        before = copy.deepcopy(self.runner.state)
        original = self.runner._clear_basket_state
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('derived failure')
        with mock.patch.object(self.runner, '_clear_basket_state', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'derived failure'):
                self.runner._sync_strategy(self.strategy)
        self.assertEqual(self.runner.state, before)

    def test_v206_derived_exception_restores_complete_state(self):
        self.seed_v206()
        before = copy.deepcopy(self.runner.state)
        lane = self.runner.v206_lane
        original = lane._reset_time_close_state
        def fail():
            original()
            raise RuntimeError('derived failure')
        with mock.patch.object(lane, '_reset_time_close_state', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'derived failure'):
                lane._sync(pd.Timestamp('2026-01-01T13:31:00Z'), None, time_actions_allowed=False)
        self.assertEqual(self.runner.state, before)

    def test_core_helper_save_is_deferred_until_complete_transition(self):
        self.seed_core()
        original = self.runner._clear_basket_state
        def helper(*args, **kwargs):
            original(*args, **kwargs)
            self.runner._save_state()
        with mock.patch.object(self.runner, '_clear_basket_state', side_effect=helper), mock.patch.object(s24, 'atomic_write_json') as writer:
            self.runner._sync_strategy(self.strategy)
        self.assertEqual(writer.call_count, 1)

    def test_core_visible_atomic_commit_is_retained_on_interrupt(self):
        self.seed_core()
        original = s24.atomic_write_json
        def fail_after_replace(path, state):
            original(path, state)
            raise KeyboardInterrupt('after replace')
        with mock.patch.object(s24, 'atomic_write_json', side_effect=fail_after_replace):
            with self.assertRaises(KeyboardInterrupt):
                self.runner._sync_strategy(self.strategy)
        self.assertEqual(self.runner._st(self.strategy)['basket'], [])
        self.assertTrue(self.runner._state_commit_is_visible())

    def test_v206_visible_commit_is_retained_on_interrupt(self):
        self.seed_v206()
        original = s24.atomic_write_json
        def fail_after_replace(path, state):
            original(path, state)
            raise KeyboardInterrupt('after replace')
        with mock.patch.object(s24, 'atomic_write_json', side_effect=fail_after_replace):
            with self.assertRaises(KeyboardInterrupt):
                self.runner.v206_lane._sync(pd.Timestamp('2026-01-01T13:31:00Z'), None, time_actions_allowed=False)
        self.assertEqual(self.runner.v206_lane.state['basket'], [])
        self.assertTrue(self.runner._state_commit_is_visible())

    def test_v206_derived_failure_containment_and_retry_preserve_single_ledger(self):
        self.seed_v206()
        lane = self.runner.v206_lane
        snapshot = copy.deepcopy(lane.state)
        with mock.patch.object(lane, '_reset_time_close_state', side_effect=RuntimeError('derived')):
            try:
                lane._sync(pd.Timestamp('2026-01-01T13:31:00Z'), None, time_actions_allowed=False)
            except RuntimeError as exc:
                self.runner._contain_v206_poll_exception(snapshot, reason='v206_poll_exception', exc=exc)
                self.runner._save_state()
        self.assertEqual(len(lane.state['basket']), 1)
        restarted = s24.S24NoAdverseRunner(params_copy())
        restarted.executor = RecordingExecutor(positions=[], orders=[])
        restarted.v206_lane._sync(pd.Timestamp('2026-01-01T13:31:00Z'), None, time_actions_allowed=False)
        self.assertEqual(restarted.v206_lane.state['basket'], [])
        with open(s24.TRADE_LOG_FILE, newline='', encoding='utf-8') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(sum(row['event'] == 'v206_close_confirmed' for row in rows), 1)

    def test_external_core_close_with_no_signal_can_replay_after_failed_save(self):
        self.seed_core()
        st = self.runner._st(self.strategy)
        st['pending_close_reason'] = None
        st['pending_close_signal_bar'] = None
        self.runner._save_state()
        with mock.patch.object(s24, 'atomic_write_json', side_effect=OSError('before replace')):
            with self.assertRaises(OSError):
                self.runner._sync_strategy(self.strategy)
        self.runner._sync_strategy(self.strategy)
        self.assertEqual(self.runner._st(self.strategy)['basket'], [])

    def test_core_restart_after_failed_save_has_no_duplicate_close_rows(self):
        self.seed_core()
        with mock.patch.object(s24, 'atomic_write_json', side_effect=OSError('before replace')):
            with self.assertRaises(OSError):
                self.runner._sync_strategy(self.strategy)
        restarted = s24.S24NoAdverseRunner(params_copy())
        restarted.executor = RecordingExecutor(positions=[], orders=[])
        restarted._sync_strategy(self.strategy)
        self.assertEqual(restarted._st(self.strategy)['basket'], [])
        with open(s24.TRADE_LOG_FILE, newline='', encoding='utf-8') as handle:
            events = [row['event'] for row in csv.DictReader(handle)]
        self.assertEqual(events.count('position_close_deal'), 1)
        self.assertEqual(events.count('position_close_confirmed'), 1)
