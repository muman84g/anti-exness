"""No MT5: exercise correlated response/claim completion with temporary files."""
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from ea_bridge import EABridgeServer


class CompletionTests(unittest.TestCase):
    def exercise(self, clear_claim):
        with tempfile.TemporaryDirectory() as folder:
            bridge = EABridgeServer(bot_suffix='s23', files_dir=folder)
            errors = []

            def responder():
                try:
                    deadline = time.monotonic() + 2
                    while not Path(bridge.cmd_file).exists():
                        if time.monotonic() > deadline:
                            raise AssertionError('No published command')
                        time.sleep(.005)
                    envelope = Path(bridge.cmd_file).read_text(encoding='utf-8')
                    request_id = envelope.split('|', 3)[1]
                    Path(bridge.claim_file).write_text(envelope, encoding='utf-8')
                    Path(bridge.cmd_file).unlink()
                    Path(bridge.res_file).write_text(
                        f'RES|{request_id}|OK|Alive|ENDRES', encoding='utf-8')
                    if clear_claim:
                        time.sleep(.2)
                        Path(bridge.claim_file).unlink()
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=responder)
            worker.start()
            start = time.monotonic()
            result = bridge.send_command('ECHO|', timeout=.5)
            elapsed = time.monotonic() - start
            claim_at_return = Path(bridge.claim_file).exists()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result, 'OK|Alive')
            self.assertLess(elapsed, 1.5)
            return claim_at_return

    def test_success_waits_for_ea_claim_completion(self):
        self.assertFalse(self.exercise(True))

    def test_stuck_claim_retains_confirmed_success_and_claim(self):
        self.assertTrue(self.exercise(False))

    def test_foreign_claim_is_not_removed_or_published_behind(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = EABridgeServer(bot_suffix='s23', files_dir=folder)
            claim = Path(bridge.claim_file)
            claim.write_text('REQ|other|1|OPEN|old', encoding='utf-8')
            self.assertEqual(bridge.send_command('ECHO|', timeout=.2), 'ERR|CLAIM_BUSY')
            self.assertEqual(claim.read_text(encoding='utf-8'), 'REQ|other|1|OPEN|old')
            self.assertFalse(Path(bridge.cmd_file).exists())

if __name__ == '__main__':
    unittest.main(verbosity=2)
