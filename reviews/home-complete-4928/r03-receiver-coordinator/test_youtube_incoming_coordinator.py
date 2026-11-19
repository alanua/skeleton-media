import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from youtube_incoming_coordinator import IncomingYouTubeCoordinator,ReceiverEventLedger

VIDEO='AAAAAAAAAAA';VIDEO2='BBBBBBBBBBB'
HASH=hashlib.sha256(VIDEO.encode()).hexdigest()

class Fixture:
    def __init__(self,root):
        self.now=100.;self.sent=[];self.published=[];self.checks=0
        self.ctx=dict(endpoint='home_edge_01',receiver_epoch='a'*64,mode_token='b'*64,
                      mode='games',revision=4,transition=False)
        self.player=dict(video_id=VIDEO,running=True,pause=False,player_state=1)
        self.event=dict(command='setVideo',video_sha256=HASH,session_digest='c'*64,
                        stream_digest='d'*64,sequence=2,observation_kind='append')
        self.ledger=ReceiverEventLedger(Path(root)/'ledger/receiver.sqlite3')
        self.after_send=lambda:None;self.on_status=lambda:None
        self.surface=True;self.overlay=True
        self.coordinator=IncomingYouTubeCoordinator(ledger=self.ledger,
            context=lambda:copy.deepcopy(self.ctx),status=self.status,handoff=self.handoff,
            verify=self.verify,publish=lambda v:self.published.append(copy.deepcopy(v)),
            clock=lambda:self.now,sleep=lambda dt:setattr(self,'now',self.now+dt))
    def status(self):
        self.on_status();return copy.deepcopy(self.player)
    def handoff(self,digest,*,expected_context,timeout):
        assert timeout==40 and expected_context==self.ctx
        self.sent.append(digest);self.ctx.update(mode='kiosk',mode_token='e'*64)
        self.after_send()
    def verify(self):
        self.checks+=1
        return dict(endpoint=self.ctx['endpoint'],receiver_epoch=self.ctx['receiver_epoch'],
                    mode=self.ctx['mode'],foreground_chrome=self.surface,
                    mpv_overlay_hidden=self.overlay,player=copy.deepcopy(self.player))
    def run(self,events=None,context=None,started=99,proofs=None):
        return self.coordinator.handle([self.event] if events is None else events,
            window_context=copy.deepcopy(self.ctx if context is None else context),
            window_started=started,request_proofs=proofs)

class CoordinatorTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        return Fixture(temp.name)
    def test_any_other_mode_transitions_directly_once_and_publishes_youtube(self):
        for mode in ('games','mpv','tv','chrome','off'):
            f=self.fixture();f.ctx['mode']=mode
            self.assertEqual(f.run()['state'],'youtube_verified')
            self.assertEqual(f.sent,[HASH]);self.assertEqual(len(f.published),1)
            self.assertEqual(f.published[0]['mode'],'kiosk')
            self.assertNotIn('mpv',json.dumps(f.published))
    def test_already_selected_youtube_needs_independent_foreground_verification(self):
        f=self.fixture();f.ctx['mode']='kiosk'
        self.assertEqual(f.run()['state'],'youtube_verified');self.assertEqual(f.sent,[])
        f=self.fixture();f.ctx['mode']='kiosk';f.surface=False
        self.assertEqual(f.run()['state'],'handoff_unverified');self.assertEqual(f.published,[])
    def test_fresh_load_can_repair_hidden_surface_even_if_mode_already_youtube(self):
        f=self.fixture();f.ctx['mode']='kiosk';f.surface=False
        f.after_send=lambda:setattr(f,'surface',True)
        self.assertEqual(f.run()['state'],'youtube_verified')
        self.assertEqual(f.sent,[HASH]);self.assertEqual(len(f.published),1)

    def test_acknowledged_handoff_without_actual_mode_or_surface_never_publishes(self):
        for target in ('mode','surface','overlay'):
            f=self.fixture()
            if target=='mode':f.after_send=lambda:f.ctx.update(mode='games')
            if target=='surface':f.surface=False
            if target=='overlay':f.overlay=False
            self.assertEqual(f.run()['state'],'handoff_unverified')
            self.assertEqual(f.published,[])
    def test_user_mode_intent_or_receiver_change_during_observation_prevents_action(self):
        for key,value in [('revision',5),('mode_token','f'*64),
                          ('receiver_epoch','f'*64),('transition',True)]:
            f=self.fixture();before=copy.deepcopy(f.ctx);f.ctx[key]=value
            self.assertEqual(f.run(context=before)['state'],'stale_context')
            self.assertEqual(f.sent,[])
    def test_context_change_while_waiting_for_clip_never_handoffs(self):
        f=self.fixture();f.player['video_id']=VIDEO2
        f.on_status=lambda:f.ctx.update(revision=5)
        self.assertEqual(f.run()['state'],'context_changed');self.assertEqual(f.sent,[])
    def test_wrong_clip_pause_noop_pairing_only_and_invalid_events_never_handoff(self):
        for changed in ('wrong_clip','paused','noop','unknown_role','bad_digest'):
            f=self.fixture()
            if changed=='wrong_clip':f.player['video_id']=VIDEO2
            if changed=='paused':f.player['pause']=True
            if changed=='noop':f.event['command']='noop'
            if changed=='unknown_role':f.event['observation_kind']='pairing'
            if changed=='bad_digest':f.event['video_sha256']='not-a-hash'
            self.assertNotEqual(f.run()['state'],'youtube_verified')
            self.assertEqual(f.sent,[]);self.assertEqual(f.published,[])
    def test_fresh_event_waits_boundedly_for_actual_matching_receiver(self):
        f=self.fixture();f.player['video_id']=VIDEO2
        def ready():
            if f.now>=100.4:f.player['video_id']=VIDEO
        f.on_status=ready
        self.assertEqual(f.run()['state'],'youtube_verified')
        self.assertLessEqual(f.now,104)
    def test_new_request_requires_independent_fresh_matching_response_proof(self):
        f=self.fixture();f.event['observation_kind']='new_request'
        self.assertEqual(f.run()['state'],'no_fresh_load')
        proof=dict(verified_response=True,session_digest='c'*64,
                   receiver_epoch='a'*64,observed_at=99.5)
        self.assertEqual(f.run(proofs={'d'*64:proof})['state'],'youtube_verified')
    def test_stale_wrong_session_or_receiver_request_proof_is_refused(self):
        for key,value in [('observed_at',98),('verified_response',False),
                          ('session_digest','f'*64),('receiver_epoch','f'*64)]:
            f=self.fixture();f.event['observation_kind']='new_request'
            proof=dict(verified_response=True,session_digest='c'*64,
                       receiver_epoch='a'*64,observed_at=99.5);proof[key]=value
            self.assertEqual(f.run(proofs={'d'*64:proof})['state'],'no_fresh_load')
            self.assertEqual(f.sent,[])
    def test_replay_dedup_survives_new_coordinator_and_ledger_instances(self):
        f=self.fixture();self.assertEqual(f.run()['state'],'youtube_verified')
        f.coordinator.ledger=ReceiverEventLedger(f.ledger.path)
        self.assertEqual(f.run()['state'],'duplicate')
        self.assertEqual(f.sent,[HASH]);self.assertEqual(len(f.published),1)
    def test_new_sequence_can_replay_same_video_without_replaying_old_event(self):
        f=self.fixture();self.assertEqual(f.run()['state'],'youtube_verified')
        f.event['sequence']=3
        self.assertEqual(f.run()['state'],'youtube_verified')
        self.assertEqual(len(f.published),2)
    def test_failed_or_uncertain_handoff_is_reserved_and_never_auto_replayed(self):
        f=self.fixture()
        def fail(*a,**k):f.sent.append(HASH);raise TimeoutError('private')
        f.coordinator.handoff=fail
        self.assertEqual(f.run()['state'],'failed')
        self.assertEqual(f.run()['state'],'duplicate')
        self.assertEqual(f.sent,[HASH]);self.assertEqual(f.published,[])
    def test_rapid_changes_select_actual_current_clip_not_list_order(self):
        f=self.fixture()
        other=dict(f.event,sequence=3,video_sha256=hashlib.sha256(VIDEO2.encode()).hexdigest())
        self.assertEqual(f.run(events=[other,f.event])['state'],'youtube_verified')
        self.assertEqual(f.sent,[HASH])
    def test_two_matching_sessions_are_ambiguous(self):
        f=self.fixture();other=dict(f.event,session_digest='f'*64)
        self.assertEqual(f.run(events=[f.event,other])['state'],'ambiguous_session')
        self.assertEqual(f.sent,[])
    def test_different_endpoint_stale_window_and_pending_transition_are_refused(self):
        for key,value in [('endpoint','samsung'),('transition',True)]:
            f=self.fixture();f.ctx[key]=value
            self.assertEqual(f.run()['state'],'stale_context');self.assertEqual(f.sent,[])
        for started in (0,101,True,float('nan')):
            f=self.fixture();self.assertEqual(f.run(started=started)['state'],'stale_context')
    def test_busy_coordinator_does_not_start_parallel_handoff(self):
        f=self.fixture();f.coordinator._lock.acquire()
        try:self.assertEqual(f.run()['state'],'busy')
        finally:f.coordinator._lock.release()
        self.assertEqual(f.sent,[])
    def test_unavailable_durable_ledger_never_falls_back_or_handoffs(self):
        f=self.fixture();f.ledger.path.parent.mkdir(mode=0o700)
        f.ledger.path.write_bytes(b'not sqlite');f.ledger.path.chmod(0o600)
        self.assertEqual(f.run()['state'],'ledger_unavailable')
        self.assertEqual(f.sent,[])

if __name__=='__main__':unittest.main()
