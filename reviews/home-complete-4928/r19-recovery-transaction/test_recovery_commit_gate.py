import threading
import unittest
from playback_intent import PlaybackIntent, PlaybackIntentChanged

class CommitGateTests(unittest.TestCase):
    def test_stale_revision_and_pending_operation_never_write(self):
        gate=PlaybackIntent();writes=[];gate.execute(['seek',1],lambda:None)
        with self.assertRaises(PlaybackIntentChanged):gate.commit(0,lambda:writes.append('bad'))
        self.assertEqual(writes,[])
        @gate.operation
        def pending():
            with self.assertRaises(PlaybackIntentChanged):gate.commit(1,lambda:writes.append('bad'))
        pending();self.assertEqual(writes,[])
    def test_busy_wire_does_not_wait_or_write(self):
        gate=PlaybackIntent();gate._wire.acquire()
        try:
            with self.assertRaises(PlaybackIntentChanged):gate.commit(0,lambda:self.fail('write'))
        finally:gate._wire.release()
    def test_callback_can_verify_revision_without_advancing_it(self):
        gate=PlaybackIntent()
        self.assertEqual(gate.commit(0,lambda:gate.read()),0)
        self.assertEqual(gate.read(),0)
    def test_callback_exception_releases_gate(self):
        gate=PlaybackIntent()
        def fail():raise OSError('synthetic')
        with self.assertRaises(OSError):gate.commit(0,fail)
        self.assertEqual(gate.commit(0,lambda:'ready'),'ready')
    def test_new_operator_mutation_runs_after_atomic_commit_not_inside_it(self):
        gate=PlaybackIntent();started=threading.Event();entered=threading.Event();allow=threading.Event();events=[]
        def commit_body():
            events.append('commit-start');entered.set();self.assertTrue(allow.wait(1))
            self.assertEqual(gate.read(),0);events.append('commit-end')
        commit=threading.Thread(target=lambda:gate.commit(0,commit_body))
        commit.start();self.assertTrue(entered.wait(1))
        def user():
            started.set();gate.execute(['seek',10],lambda:events.append('seek'))
        operator=threading.Thread(target=user);operator.start();self.assertTrue(started.wait(1))
        self.assertEqual(events,['commit-start'])
        allow.set();commit.join(1);operator.join(1)
        self.assertFalse(commit.is_alive());self.assertFalse(operator.is_alive())
        self.assertEqual(events,['commit-start','commit-end','seek'])
        self.assertEqual(gate.read(),1)
    def test_invalid_revision_or_callback_never_applies(self):
        gate=PlaybackIntent()
        for rev,callback in [(True,lambda:None),(-1,lambda:None),(0,None)]:
            with self.assertRaises(ValueError):gate.commit(rev,callback)

if __name__=='__main__':unittest.main()
