"""Cadence transaction behaviors with synthetic display/MPV adapters only."""
import copy
from contextlib import nullcontext
import unittest
from cadence_policy import CadencePolicy,constant_pts_rate

class Fixture:
    def __init__(self):
        self.now=100.;self.position=100.;self.state={};self.actions=[];self.audits=[]
        self.context=dict(endpoint='home_edge_01',mode='mpv',session='a'*64,
            process=('boot',42,'100'),revision=3,job_id='b'*16,source_id='s',
            path_sha256='c'*64,policy_generation='f'*64,live=False,pause=False,
            seeking=False,paused_for_cache=False,transition=False,operator_override=False,loaded=True)
        self.monitor=dict(identity='d'*64,logical_count=1,physical_count=1,width=1920,height=1080,
            hz=60.,mode_id='m60',supported={'50':'m50','60':'m60'},layout_digest='e'*64)
        self.config=dict(schema='skeleton.cadence.opt_in.v1',enabled=True,endpoint='home_edge_01',
            approval_receipt='1'*64,operator_approval_ref='fixture:explicit-opt-in',
            display_identity='d'*64,generation='f'*64)
        self.points=[i/25 for i in range(26)];self.on_sample=lambda:None
        self.on_admit=lambda request:None;self.no_apply=False;self.freeze=False
        self.after_apply=lambda:None;self.raise_after_apply=False;self.gate_allowed=True
        self.rollback_allowed=True;self.reservation_applies=True
    def snapshot(self):return dict(copy.deepcopy(self.context),position=self.position)
    def sample(self,timeout):
        assert timeout==3
        self.now+=1;self.position+=1;self.on_sample()
        return list(self.points)
    def write(self,value):
        if self.reservation_applies:self.state=copy.deepcopy(value)
    def admit(self,request):
        self.on_admit(request)
        allowed=self.gate_allowed and (self.rollback_allowed or not request['request_id'].endswith('-rollback'))
        return dict(allowed=allowed,operation_id=request['operation_id'],
                    approval_receipt=self.config['approval_receipt'])
    def execute(self,request):
        self.actions.append(copy.deepcopy(request))
        if not self.no_apply:
            target=int(request['operation_id'].split('_')[-2])
            self.monitor.update(hz=float(target),mode_id='m'+str(target))
        self.after_apply()
        if self.raise_after_apply:raise TimeoutError('synthetic')
    def sleep(self,value):
        self.now+=value
        if not self.freeze:self.position+=value
    def run(self):
        return CadencePolicy().run(config=self.config,snapshot=self.snapshot,
            display=lambda:copy.deepcopy(self.monitor),sample_pts=self.sample,
            read_state=lambda:copy.deepcopy(self.state),write_state=self.write,
            admit=self.admit,execute=self.execute,mutation_guard=nullcontext,
            clock=lambda:self.now,sleep=self.sleep,audit=lambda v:self.audits.append(copy.deepcopy(v)))

class CadenceTests(unittest.TestCase):
    def test_25_at_60_uses_registered_50_operation_and_verifies_actual_progress(self):
        f=Fixture();result=f.run()
        self.assertEqual(result['state'],'applied_and_progress_verified')
        self.assertTrue(result['progress_verified']);self.assertEqual(result['hz'],50)
        self.assertEqual([r['operation_id'] for r in f.actions],['media_display_refresh_50_v1'])
        self.assertEqual(f.actions[0]['node_id'],'home-edge-01')
        self.assertEqual(f.state['owned_mode_id'],'m50')
        self.assertEqual([a['state'] for a in f.audits],['reserved_before_execution','applied_and_progress_verified'])
    def test_25_at_50_is_noop(self):
        f=Fixture();f.monitor.update(hz=50.,mode_id='m50')
        self.assertEqual(f.run()['state'],'already_matched');self.assertEqual(f.actions,[])
    def test_30_and_60_at_60_are_noop_and_30_at_50_restores_60(self):
        for fps in (30,60):
            f=Fixture();f.points=[i/fps for i in range(fps+1)]
            self.assertEqual(f.run()['state'],'already_matched');self.assertEqual(f.actions,[])
        f=Fixture();f.points=[i/30 for i in range(31)];f.monitor.update(hz=50.,mode_id='m50')
        self.assertEqual(f.run()['hz'],60)
    def test_disabled_policy_never_reads_or_acts(self):
        f=Fixture();f.config['enabled']=False
        f.snapshot=lambda:(_ for _ in ()).throw(AssertionError('read'))
        self.assertEqual(f.run()['state'],'disabled');self.assertEqual(f.actions,[])
    def test_optin_receipt_and_bound_display_are_required(self):
        for key,value in [('approval_receipt',''),('operator_approval_ref',''),
                          ('generation',''),('endpoint','other')]:
            f=Fixture();f.config[key]=value
            self.assertEqual(f.run()['state'],'approval_missing');self.assertEqual(f.actions,[])
    def test_unsupported_50_never_changes_resolution_or_connector(self):
        f=Fixture();f.monitor['supported'].pop('50')
        self.assertEqual(f.run()['state'],'unsupported');self.assertEqual(f.actions,[])
    def test_unreliable_vfr_unknown_and_duplicate_pts_are_refused(self):
        for values in [[i/24 for i in range(25)],[0.]*25,
                       [i*.04+(i%2)*.005 for i in range(26)],
                       [0.,None]*15,[0.,float('inf')]*15,[True]*25]:
            f=Fixture();f.points=values
            self.assertEqual(f.run()['state'],'cadence_unreliable');self.assertEqual(f.actions,[])
    def test_other_endpoint_chrome_tv_live_pause_buffer_seek_and_override_are_ineligible(self):
        for key,value in [('endpoint','samsung'),('mode','kiosk'),('live',True),
                          ('pause',True),('paused_for_cache',True),('seeking',True),
                          ('transition',True),('operator_override',True)]:
            f=Fixture();f.context[key]=value
            self.assertEqual(f.run()['state'],'ineligible');self.assertEqual(f.actions,[])
    def test_session_source_mode_revision_and_policy_change_during_sampling_cancel(self):
        for key,value in [('session','2'*64),('source_id','new'),('mode','games'),
                          ('revision',4),('policy_generation','2'*64),('seeking',True)]:
            f=Fixture();f.on_sample=lambda key=key,value=value:f.context.update({key:value})
            self.assertEqual(f.run()['state'],'sample_unverified');self.assertEqual(f.actions,[])
    def test_wrong_or_multiple_display_is_refused(self):
        for key,value in [('identity','2'*64),('logical_count',2),('physical_count',2)]:
            f=Fixture();f.monitor[key]=value
            self.assertEqual(f.run()['state'],'display_mismatch');self.assertEqual(f.actions,[])
    def test_gate_blocked_or_mismatched_receipt_never_executes(self):
        f=Fixture();f.gate_allowed=False
        self.assertEqual(f.run()['state'],'action_gate_blocked');self.assertEqual(f.actions,[])
        f=Fixture();f.admit=lambda request:dict(allowed=True,operation_id=request['operation_id'],approval_receipt='2'*64)
        self.assertEqual(f.run()['state'],'action_gate_blocked');self.assertEqual(f.actions,[])
    def test_display_layout_change_after_admission_prevents_actuation(self):
        f=Fixture();f.on_admit=lambda _:f.monitor.update(layout_digest='2'*64)
        self.assertEqual(f.run()['state'],'context_changed');self.assertEqual(f.actions,[])
    def test_cooldown_and_two_attempt_bound_survive_new_engine(self):
        f=Fixture();f.state=dict(session='a'*64,boot='boot',attempts=1,last_attempt=90.)
        self.assertEqual(f.run()['state'],'cooldown');self.assertEqual(f.actions,[])
        f=Fixture();f.state=dict(session='a'*64,boot='boot',attempts=2,last_attempt=0.)
        self.assertEqual(f.run()['state'],'retry_exhausted');self.assertEqual(f.actions,[])
        f=Fixture();f.state=dict(session='a'*64,boot='boot',attempts=1,last_attempt=1000.)
        self.assertEqual(f.run()['state'],'cooldown')
    def test_new_boot_does_not_inherit_incompatible_monotonic_clock(self):
        f=Fixture();f.state=dict(session='other',boot='old-boot',attempts=1,last_attempt=100000.)
        self.assertEqual(f.run()['state'],'applied_and_progress_verified')
    def test_manual_display_override_in_same_session_is_preserved(self):
        f=Fixture();f.state=dict(session='a'*64,boot='boot',attempts=1,last_attempt=0.,owned_mode_id='manual')
        self.assertEqual(f.run()['state'],'operator_override');self.assertEqual(f.actions,[])
    def test_acknowledgement_without_applied_display_is_not_success(self):
        f=Fixture();f.no_apply=True
        result=f.run();self.assertEqual(result['state'],'verification_failed');self.assertFalse(result['applied'])
        self.assertEqual(len(f.actions),1)
    def test_applied_display_without_progress_rolls_back_only_under_same_context(self):
        f=Fixture();f.freeze=True
        result=f.run();self.assertEqual(result['state'],'verification_failed')
        self.assertTrue(result['rollback_verified']);self.assertFalse(result['applied'])
        self.assertEqual([r['operation_id'] for r in f.actions],
                         ['media_display_refresh_50_v1','media_display_refresh_60_v1'])
    def test_new_user_intent_after_trial_prevents_rollback_overwrite(self):
        f=Fixture();f.after_apply=lambda:f.context.update(revision=4)
        result=f.run();self.assertEqual(result['state'],'verification_failed')
        self.assertFalse(result['rollback_verified']);self.assertTrue(result['applied'])
        self.assertEqual(len(f.actions),1)
    def test_rollback_needs_its_own_approved_operation(self):
        f=Fixture();f.freeze=True;f.rollback_allowed=False
        result=f.run();self.assertFalse(result['rollback_verified']);self.assertTrue(result['applied'])
        self.assertEqual(len(f.actions),1)
    def test_failed_command_can_only_succeed_with_independent_state_and_progress(self):
        f=Fixture();f.raise_after_apply=True
        self.assertEqual(f.run()['state'],'applied_and_progress_verified')
        self.assertTrue(f.audits[-1]['command_error'])
    def test_unapplied_reservation_prevents_command(self):
        f=Fixture();f.reservation_applies=False
        self.assertEqual(f.run()['state'],'reservation_unverified');self.assertEqual(f.actions,[])
    def test_corrupt_persisted_retry_state_is_refused(self):
        for state in [{'attempts':True},{'attempts':-1},{'last_attempt':float('nan')}]:
            f=Fixture();f.state=state
            self.assertEqual(f.run()['state'],'state_unavailable');self.assertEqual(f.actions,[])

if __name__=='__main__':unittest.main()
