"""File-backed journal partition only; native restart parity is a separate test."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shadow_state as state

IDENTITY={'source':'fixed-fixture-v001','config':'fixture','run':'journal-case-v001'}
Q1={'event':1000,'bid':'1.00','ask':'1.01'}
Q2={'event':2000,'bid':'1.02','ask':'1.03'}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)

    def journal(self):
        return state.QuoteJournal(self.path,IDENTITY)

    def test_file_commit_and_fresh_replay(self):
        j=self.journal(); j.recover(lambda _:self.fail('empty journal callback'))
        j.preflight(Q1,1000); j.commit(Q1,state.digest(Q1),1000)
        j.preflight(Q2,1000); j.commit(Q2,state.digest(Q2),1000)
        seen=[]
        recovered=self.journal(); recovered.recover(lambda q:(seen.append(q),state.digest(q))[1])
        self.assertEqual(seen,[Q1,Q2]);self.assertEqual(recovered.count,2)
        self.assertEqual(recovered.chain,j.chain)
        saved=state.parse(recovered.checkpoint.read_bytes())
        self.assertEqual(saved['business_digest'],state.digest(Q2))

    def test_partial_tail_preserved_and_terminal(self):
        j=self.journal();j.recover(lambda _:None);j.commit(Q1,state.digest(Q1),1000)
        with j.path.open('ab') as f:f.write(b'{"partial":')
        before=j.path.read_bytes();r=self.journal()
        with self.assertRaisesRegex(state.JournalError,'partial_or_oversize'):r.recover(state.digest)
        self.assertEqual(j.path.read_bytes(),before)
        with self.assertRaisesRegex(state.JournalError,'not_ready'):r.commit(Q2,state.digest(Q2),1000)

    def test_complete_tail_replays_after_checkpoint_failure(self):
        j=self.journal();j.recover(lambda _:None);j.commit(Q1,state.digest(Q1),1000)
        saved=j.checkpoint.read_bytes()
        j.commit(Q2,state.digest(Q2),1000)
        # Simulates a completed journal append with the preceding committed checkpoint.
        j.checkpoint.write_bytes(saved)
        r=self.journal();r.recover(state.digest)
        self.assertEqual(r.count,2);self.assertEqual(r.business_digest,state.digest(Q2))

    def test_wrong_business_digest_and_identity_rejected(self):
        j=self.journal();j.recover(lambda _:None);j.commit(Q1,state.digest(Q1),1000)
        poisoned=self.journal()
        with self.assertRaisesRegex(state.JournalError,'digest_mismatch'):poisoned.recover(lambda _:state.ZERO)
        with self.assertRaisesRegex(state.JournalError,'fresh_instance'):poisoned.recover(lambda _:self.fail('terminal repeated callback'))
        wrong=state.QuoteJournal(self.path,dict(IDENTITY,config='different'))
        with self.assertRaisesRegex(state.JournalError,'identity_or_schema'):wrong.recover(state.digest)

    def test_actual_checkpoint_publish_failure_and_fresh_recovery(self):
        j=self.journal();j.recover(lambda _:None);j.commit(Q1,state.digest(Q1),1000)
        with patch.object(state.os,'replace',side_effect=OSError('fixture replace failure')):
            with self.assertRaisesRegex(state.JournalError,'checkpoint_write_failed'):j.commit(Q2,state.digest(Q2),1000)
        pending=j.checkpoint.with_name(j.checkpoint.name+'.new')
        self.assertTrue(pending.exists())
        r=self.journal();r.recover(state.digest)
        self.assertEqual(r.count,2);self.assertFalse(pending.exists())
        self.assertEqual(state.parse(r.checkpoint.read_bytes())['business_digest'],state.digest(Q2))

    def test_record_bound_before_control_and_nonfinite_reader(self):
        j=self.journal();j.recover(lambda _:None)
        with self.assertRaisesRegex(state.JournalError,'cap_preserve_history'):j.preflight({'oversize':'x'*5000},1000)
        self.assertFalse(j.path.exists())
        for raw in (b'{"x":NaN}',b'{"x":1e999}',b'{"x":1,"x":2}'):
            with self.assertRaises(state.JournalError):state.parse(raw)

    def test_state_owner_exclusion_and_release(self):
        with state.StateLock(self.path):
            with self.assertRaisesRegex(state.JournalError,'state_owner_busy'):
                with state.StateLock(self.path):pass
        with state.StateLock(self.path):pass


if __name__=='__main__':unittest.main()
