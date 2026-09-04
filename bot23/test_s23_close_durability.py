"""Re-observed close rows must be synced before they authorize state progress."""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import live_s23_bot as s23


class CloseReplayDurabilityTests(unittest.TestCase):
    def test_malformed_quoted_close_row_cannot_authorize_replay(self):
        row = dict(event='position_close_confirmed', deal_id=99001,
                   lane_id=1, position_identifier=7001, note='safe')
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'trades.csv'
            s23.append_csv(str(path), row, s23.TRADE_FIELDS)
            original = path.read_bytes()
            self.assertTrue(original.endswith(b'safe\r\n'))
            for malformed in (b'"unterminated\n', b'"closed"junk\n'):
                with self.subTest(malformed=malformed):
                    path.write_bytes(original[:-len(b'safe\r\n')] + malformed)
                    with self.assertRaises((csv.Error, RuntimeError)):
                        s23.confirmed_close_audit_exists(str(path), row, s23.TRADE_FIELDS)

    def test_valid_multiline_quoted_close_row_remains_replayable(self):
        row = dict(event='position_close_confirmed', deal_id=99001,
                   lane_id=1, position_identifier=7001, note='first\nsecond "quoted"')
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'trades.csv')
            s23.append_csv(path, row, s23.TRADE_FIELDS)
            self.assertTrue(s23.confirmed_close_audit_exists(path, row, s23.TRADE_FIELDS))

    def test_confirmed_replay_fsyncs_existing_ledger(self):
        row = dict(event='position_close_confirmed', deal_id=99001,
                   lane_id=1, position_identifier=7001)
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'trades.csv')
            s23.append_csv(path, row, s23.TRADE_FIELDS)
            with mock.patch.object(s23.os, 'fsync') as sync, mock.patch.object(s23, '_fsync_parent_directory') as parent:
                self.assertTrue(s23.confirmed_close_audit_exists(path, row, s23.TRADE_FIELDS))
                self.assertGreaterEqual(sync.call_count, 1)
                parent.assert_called_once()

    def test_confirmed_replay_sync_failure_propagates(self):
        row = dict(event='position_close_confirmed', deal_id=99001,
                   lane_id=1, position_identifier=7001)
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'trades.csv')
            s23.append_csv(path, row, s23.TRADE_FIELDS)
            with mock.patch.object(s23.os, 'fsync', side_effect=OSError('sync failed')):
                with self.assertRaisesRegex(OSError, 'sync failed'):
                    s23.confirmed_close_audit_exists(path, row, s23.TRADE_FIELDS)
