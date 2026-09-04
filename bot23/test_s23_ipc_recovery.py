"""No-order recovery tests: synthetic broker and isolated runtime state."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch
import live_s23_bot
import live_executor
import pandas as pd
import test_s23_regressions as fixtures

setUpModule = fixtures.setUpModule
tearDownModule = fixtures.tearDownModule


class RecoveryTests(unittest.TestCase):
    def test_recovered_close_claim_returns_unresolved_before_any_execution(self):
        source = Path(__file__).with_name('BotBridge_s23.mq5').read_text(encoding='utf-8')
        timer = source.split('void OnTimer()', 1)[1]
        self.assertIn('bool recovered_close = recovered_claim && StringFind(command, "CLOSE|") == 0;', timer)
        self.assertIn('if(recovered_close)', timer)
        block = timer.split('if(recovered_close)', 1)[1].split('if(request_expired', 1)[0]
        self.assertNotIn('HandleCommand', block)
        self.assertIn('ERR|CLOSE_RESULT_UNRESOLVED', block)
        self.assertIn('if(WriteResponse(', block)
        self.assertIn('ClearClaim();', block)
        self.assertIn('return;', block)
        self.assertLess(timer.index('if(recovered_close)'), timer.index('HandleCommand(command)'))

    def test_recovered_close_receipt_is_not_definitive_no_fill(self):
        response = 'ERR|CLOSE_RESULT_UNRESOLVED'
        with patch.object(live_executor.ea_bridge, 'send_command', return_value=response):
            result = live_executor.MT5Executor().close_position(
                7701, expected_login=123456, expected_server='test-only',
                expected_symbol='XAUUSD', expected_magic=230023,
                expected_comment='s23_za_l1', expected_identifier=7701)
        self.assertFalse(result.success)
        self.assertEqual(result.raw_response, response)
        self.assertFalse(live_s23_bot.S23HorizontalInventoryRunner._close_result_definitive_no_fill(result))

    def test_recovered_close_result_survives_restart_without_resend(self):
        runner, strategy, state = fixtures.make_runner(live=True)
        executor = fixtures.CountingExecutor()
        runner.executor = executor
        fixtures.arm_owned_basket(strategy, state, executor, ticket=96145)
        executor.close_position = lambda ticket, *_args, **_kwargs: (
            executor.close_calls.append(ticket) or live_executor.CloseResult(
                False, 'FAILED', raw_response='ERR|CLOSE_RESULT_UNRESOLVED'))
        row = pd.Series({'Open':99.0,'Close':99.0,'AskOpen':99.03}, name=pd.Timestamp('2026-08-25T13:11:00Z'))
        self.assertEqual(runner._close_basket(strategy,'basket_stop',row,-2.0), 'failed')
        self.assertEqual(executor.close_calls, [96145])
        restarted, restarted_strategy, _ = fixtures.make_runner(live=True)
        restarted.state = copy.deepcopy(runner.state)
        restarted.executor = executor
        self.assertFalse(restarted._sync_strategy(restarted_strategy))
        self.assertEqual(restarted._close_basket(restarted_strategy,'basket_stop',row,-2.0),'failed')
        self.assertEqual(executor.close_calls,[96145])
        self.assertIsNotNone(restarted._st(restarted_strategy)['basket'][0]['close_submission_started_utc'])

    def test_v32_bridge_cannot_start_v33_runner(self):
        runner, _, _ = fixtures.make_runner(live=False)
        runner.dm.connect = lambda: True
        runner.executor = live_s23_bot.FakeExecutor()
        caps = runner.executor.get_bridge_capabilities()
        caps['version'] = '2026-09-04-s23-legacy-query-v32'
        runner.executor.get_bridge_capabilities = lambda: caps
        with patch.object(live_s23_bot,'validate_csv_schema',return_value=None), patch.object(runner,'_preflight_reject',return_value=False) as reject:
            self.assertFalse(runner.connect_and_preflight())
        reject.assert_called_once_with('bridge_version_mismatch')

    def blocked_runner(self, error='ERR|CLAIM_BUSY'):
        runner, strategy, state = fixtures.make_runner(live=True)
        runner.executor = fixtures.CountingExecutor()
        runner._set_sync_block(strategy, 'ipc_open_not_published', {'error': error}, recoverable=False)
        return runner, strategy, state

    def test_legacy_unsent_recovers_only_after_two_clean_syncs(self):
        runner, strategy, state = self.blocked_runner()
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertTrue(state['sync_block_new_entries'])
        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(state['sync_block_new_entries'])
        self.assertEqual(runner.executor.open_calls, 0)

    def test_query_failure_breaks_legacy_confirmation_streak(self):
        runner, strategy, state = self.blocked_runner()
        runner._sync_strategy(strategy)
        runner.executor.orders_available = False
        runner._sync_strategy(strategy)
        runner.executor.orders_available = True
        self.assertFalse(runner._sync_strategy(strategy))
        self.assertTrue(runner._sync_strategy(strategy))

    def test_unknown_or_missing_no_fill_evidence_stays_blocked(self):
        for error in ('ERR|TIMEOUT', 'ERR|UNKNOWN', None, ['ERR|CLAIM_BUSY']):
            with self.subTest(error=error):
                runner, strategy, state = self.blocked_runner(error)
                for _ in range(3):
                    runner._sync_strategy(strategy)
                self.assertTrue(state['sync_block_new_entries'])

    def test_unmanaged_position_never_clears_legacy_block(self):
        runner, strategy, state = self.blocked_runner()
        fixtures.arm_owned_basket(strategy, state, runner.executor)
        state['basket'] = []
        runner._sync_strategy(strategy)
        self.assertTrue(state['sync_block_new_entries'])
        self.assertEqual(state['sync_block_reason'], 'live_positions_without_state')

    def test_new_unsent_block_clears_on_exact_active_owned_sync(self):
        runner, strategy, state = self.blocked_runner()
        fixtures.arm_owned_basket(strategy, state, runner.executor)
        state['sync_block_recoverable'] = True
        self.assertTrue(runner._sync_strategy(strategy))
        self.assertFalse(state['sync_block_new_entries'])
        self.assertEqual(len(state['basket']), 1)
        self.assertEqual(runner.executor.open_calls, 0)

    def test_definite_unsent_route_releases_reservation_but_timeout_consumes(self):
        for error, retry in (('ERR|CLAIM_BUSY', True), ('ERR|TIMEOUT', False)):
            with self.subTest(error=error):
                runner, strategy, state = fixtures.make_runner(live=True)
                runner.executor = fixtures.CountingExecutor()
                runner.executor.last_order_error = error
                opportunity, row, at, info = fixtures.sample_opportunity()
                opportunity['source'] = 'za'
                saved = []
                runner._save_state = lambda: saved.append(copy.deepcopy(runner.state))
                readiness = {1: (True, 'ready', False)}
                with patch.object(live_s23_bot, 'utc_now', return_value=at.to_pydatetime()):
                    runner._route_opportunity(opportunity, row, info, at, readiness)
                self.assertEqual(runner.executor.open_calls, 1)
                if retry:
                    self.assertIsNone(runner.state['routing']['last_routed_signal_bar'])
                    self.assertIsNone(state['pending_open_opportunity_id'])
                    self.assertIsNone(saved[-1]['routing']['last_routed_signal_bar'])
                else:
                    self.assertEqual(runner.state['routing']['last_consumed_lane_id'], 1)
                    self.assertEqual(runner.state['routing']['last_routed_signal_bar'], opportunity['event_time'])
                    self.assertIsNotNone(state['pending_open_opportunity_id'])

    def test_later_consuming_lane_prevents_retry_release(self):
        runner, strategy, state = fixtures.make_runner(live=True)
        opportunity, row, at, info = fixtures.sample_opportunity()
        opportunity['source'] = 'za'
        state.update(sync_block_new_entries=True, sync_block_reason='positions_unavailable', sync_block_recoverable=True)
        with patch.object(runner, '_consume_opportunity', side_effect=[(False, 'entry_final_guard'), (True, 'pending_armed')]):
            runner._route_opportunity(opportunity, row, info, at, {1:(True,'ready',False),2:(True,'ready',False)})
        self.assertEqual(runner.state['routing']['last_consumed_lane_id'], 2)
        self.assertEqual(runner.state['routing']['last_routed_signal_bar'], opportunity['event_time'])

    def test_strategy_rejection_is_not_reclassified_as_retry(self):
        runner, strategy, state = fixtures.make_runner(live=True)
        opportunity, row, at, info = fixtures.sample_opportunity()
        opportunity['source'] = 'za'
        with patch.object(runner, '_consume_opportunity', return_value=(False, 'za_not_extreme')):
            runner._route_opportunity(opportunity, row, info, at, {1:(True,'ready',False)})
        self.assertEqual(runner.state['routing']['last_routed_signal_bar'], opportunity['event_time'])

    def test_readiness_query_failure_releases_only_when_no_receipt_remains(self):
        for orphan_receipt in (False, True):
            runner, strategy, state = fixtures.make_runner(live=True)
            opportunity, row, at, info = fixtures.sample_opportunity()
            opportunity['source'] = 'za'
            state.update(sync_block_new_entries=True, sync_block_reason='positions_unavailable', sync_block_recoverable=True)
            if orphan_receipt:
                state['pending_open_started_utc'] = opportunity['decision_time']
            runner._route_opportunity(opportunity, row, info, at, {1:(False,'positions_unavailable',False)})
            expected = opportunity['event_time'] if orphan_receipt else None
            self.assertEqual(runner.state['routing']['last_routed_signal_bar'], expected)

if __name__ == '__main__':
    unittest.main(verbosity=2)
