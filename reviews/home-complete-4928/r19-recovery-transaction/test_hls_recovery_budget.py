import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from hls_recovery_budget import SQLiteRecoveryBudget
from hls_recovery_transaction import HLSRecovery
from test_hls_recovery_transaction import Fixture

class DurableBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'budget.sqlite3'
        self.session='a'*64
    def test_restart_does_not_reset_attempt_or_cooldown(self):
        self.assertEqual(SQLiteRecoveryBudget(self.path).reserve(self.session,100),{'state':'reserved','attempt':1})
        self.assertEqual(SQLiteRecoveryBudget(self.path).reserve(self.session,101)['state'],'cooldown')
        self.assertEqual(SQLiteRecoveryBudget(self.path).reserve(self.session,160),{'state':'reserved','attempt':2})
        self.assertEqual(SQLiteRecoveryBudget(self.path).reserve(self.session,999)['state'],'retry_exhausted')
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
    def test_invalid_or_backward_clock_never_resets_retry(self):
        budget=SQLiteRecoveryBudget(self.path);budget.reserve(self.session,100)
        self.assertEqual(budget.reserve(self.session,0)['state'],'cooldown')
        for value in [True,float('inf'),float('nan'),-1]:
            self.assertEqual(budget.reserve(self.session,value)['state'],'budget_unavailable')
    def test_other_session_does_not_delete_prior_budget(self):
        budget=SQLiteRecoveryBudget(self.path);budget.reserve(self.session,100)
        budget.reserve('b'*64,100)
        self.assertEqual(budget.reserve(self.session,101)['state'],'cooldown')
    def test_sqlite_lock_failure_never_falls_back_to_memory(self):
        budget=SQLiteRecoveryBudget(self.path);budget.reserve(self.session,100)
        db=sqlite3.connect(self.path);db.execute('BEGIN IMMEDIATE')
        try:self.assertEqual(budget.reserve(self.session,200)['state'],'budget_unavailable')
        finally:db.rollback();db.close()
    def test_corrupt_database_and_nonprivate_storage_are_refused(self):
        self.path.write_bytes(b'bad sqlite');self.path.chmod(0o600)
        self.assertEqual(SQLiteRecoveryBudget(self.path).reserve(self.session,100)['state'],'budget_unavailable')
        self.path.unlink();self.path.write_bytes(b'');self.path.chmod(0o644)
        self.assertEqual(SQLiteRecoveryBudget(self.path).reserve(self.session,100)['state'],'budget_unavailable')
    def test_capacity_fails_closed_without_eviction_of_active_session(self):
        b=SQLiteRecoveryBudget(self.path)
        for index in range(128):self.assertEqual(b.reserve(hashlib.sha256(str(index).encode()).hexdigest(),100)['state'],'reserved')
        self.assertEqual(b.reserve(self.session,100)['state'],'session_capacity')
    def test_resolver_failure_consumes_durable_budget_across_engine_instances(self):
        f=Fixture();f.state['session']=self.session
        def fail(*a,**kw):raise RuntimeError('private')
        f.resolve=fail
        def run():
            return f.run(HLSRecovery(),reserve_budget=lambda state,now:SQLiteRecoveryBudget(self.path).reserve(state['session'],now))
        self.assertEqual(run()['state'],'failed')
        self.assertEqual(run()['state'],'cooldown')
        f.t+=60;self.assertEqual(run()['state'],'failed')
        f.t+=60;self.assertEqual(run()['state'],'retry_exhausted')
        self.assertEqual(f.commands,[])
    def test_unavailable_budget_prevents_resolve_and_player_mutation(self):
        f=Fixture()
        r=f.run(reserve_budget=lambda state,now:{'state':'budget_unavailable'})
        self.assertEqual(r['state'],'budget_unavailable');self.assertEqual(f.calls,0);self.assertEqual(f.commands,[])

if __name__=='__main__':unittest.main()
