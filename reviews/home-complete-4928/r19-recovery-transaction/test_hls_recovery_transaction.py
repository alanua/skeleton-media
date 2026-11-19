import copy
import unittest
from contextlib import nullcontext
from hls_recovery_transaction import HLSRecovery


class Fixture:
    def __init__(self):
        self.t=100.;self.rev=0;self.commands=[];self.commits=[];self.calls=0
        self.job={'id':'j','sources':[]}
        self.source={'source_id':'s','url':'https://example.invalid/old.m3u8','kind':'hls'}
        self.state=dict(endpoint='home_edge_01',mode='mpv',session='p1',
            job_id='j',source_id='s',season='1',episode='2',voice='uk',
            sid='no',aid=1,sub_visibility=False,position=42.,pause=False,
            seeking=False,paused_for_cache=True,live=False,frames=10,path=self.source['url'])
        self.fresh=dict(url='https://example.invalid/new.m3u8',source_id='new',
            season='1',episode='2',group='uk')
        self.hook=lambda:None
        self.progress=True
    def snapshot(self):
        self.hook();return copy.deepcopy(self.state)
    def sleep(self,seconds):
        self.t+=seconds
        if self.commands and self.state['pause'] is False and self.progress:
            self.state['position']+=seconds;self.state['frames']+=25
            self.state['paused_for_cache']=False
    def resolve(self,job,source,timeout):
        assert timeout==12
        job['refreshed']=True
        self.calls+=1
        return self.fresh
    def command(self,parts,timeout,expected_revision):
        assert timeout==2 and expected_revision==self.rev
        self.commands.append(parts)
        if parts[0]=='loadfile':
            self.state.update(path=parts[1],loaded=True,pause=True)
        else:self.state['pause']=parts[2]
        return {'error':'success'}
    def commit(self,job,source,expected_revision):
        assert expected_revision==self.rev
        self.commits.append((job,source))
    def run(self,engine=None,**kwargs):
        return (engine or HLSRecovery()).run(job=self.job,source=self.source,
            snapshot=self.snapshot,revision=lambda:self.rev,resolve=self.resolve,
            command=self.command,mutation_guard=nullcontext,commit=self.commit,
            clock=lambda:self.t,sleep=self.sleep,**kwargs)


class TransactionTests(unittest.TestCase):
    def test_real_transaction_calls_resolve_load_resume_verify_commit(self):
        f=Fixture();r=f.run()
        self.assertEqual(r['state'],'recovered')
        self.assertEqual([c[0] for c in f.commands],['loadfile','set_property'])
        options=f.commands[0][-1]
        self.assertEqual(options['start'],'42.0')
        self.assertEqual(options['pause'],'yes')
        self.assertEqual(options['aid'],'1')
        self.assertEqual(options['sid'],'no')
        self.assertEqual(options['sub-visibility'],'no')
        self.assertNotIn('refreshed',f.job)
        self.assertTrue(f.commits[0][0]['refreshed'])
    def test_ineligible_states_never_resolve_or_mutate(self):
        for field,value in [('mode','kiosk'),('endpoint','samsung'),('pause',True),
                ('seeking',True),('live',True),('session',''),('job_id','other'),
                ('source_id','other'),('position',float('nan')),('paused_for_cache',False)]:
            with self.subTest(field=field):
                f=Fixture();f.state[field]=value
                self.assertEqual(f.run()['state'],'ineligible')
                self.assertEqual((f.calls,f.commands),(0,[]))
    def test_cancel_during_resolver_preserves_user_intent(self):
        for field,value in [('mode','games'),('session','new'),('position',90),
                ('sid',2),('aid',2),('voice','de'),('episode','3')]:
            with self.subTest(field=field):
                f=Fixture();resolve=f.resolve
                def changed(*args,**kw):
                    result=resolve(*args,**kw);f.state[field]=value;return result
                f.resolve=changed
                self.assertEqual(f.run()['state'],'cancelled')
                self.assertEqual(f.commands,[])
    def test_pause_requested_while_load_returns_prevents_unpause(self):
        f=Fixture();command=f.command
        def pause(*args,**kw):
            result=command(*args,**kw);f.rev+=1;return result
        f.command=pause
        self.assertEqual(f.run()['state'],'cancelled')
        self.assertEqual(len(f.commands),1)
        self.assertEqual(f.commits,[])
    def test_wrong_source_voice_drm_and_episode_rejected(self):
        for field,value in [('group','de'),('season','2'),('episode','9'),
                ('has_drm',True),('is_trailer',True),('live',True),('url','')]:
            with self.subTest(field=field):
                f=Fixture();f.fresh[field]=value
                self.assertEqual(f.run()['state'],'cancelled')
                self.assertEqual(f.commands,[])
    def test_missing_frames_never_count_as_success(self):
        f=Fixture();f.state['frames']=None
        def no_frames(seconds):
            f.t+=seconds
            if f.commands and not f.state['pause']:
                f.state['position']+=seconds;f.state['paused_for_cache']=False
        f.sleep=no_frames
        self.assertEqual(f.run()['state'],'progress_unverified')
        self.assertEqual(f.commits,[])
    def test_loaded_without_progress_is_not_success(self):
        f=Fixture();f.progress=False
        self.assertEqual(f.run()['state'],'progress_unverified')
        self.assertEqual(f.commits,[])
    def test_retry_and_cooldown_include_resolver_failure(self):
        f=Fixture();engine=HLSRecovery()
        def fail(*args,**kw):raise RuntimeError('private signed URL must not leak')
        f.resolve=fail
        self.assertEqual(f.run(engine)['state'],'failed')
        self.assertEqual(f.run(engine)['state'],'cooldown')
        f.t+=60
        self.assertEqual(f.run(engine)['state'],'failed')
        f.t+=60
        self.assertEqual(f.run(engine)['state'],'retry_exhausted')
    def test_session_budgets_are_not_reset_by_other_sessions(self):
        f=Fixture();e=HLSRecovery()
        def fail(*args,**kw):raise RuntimeError()
        f.resolve=fail
        f.run(e);f.state['session']='p2';f.run(e)
        f.state['session']='p1'
        self.assertEqual(f.run(e)['state'],'cooldown')
    def test_busy_does_not_mutate(self):
        f=Fixture();e=HLSRecovery();e._lock.acquire()
        try:self.assertEqual(f.run(e)['state'],'busy')
        finally:e._lock.release()
        self.assertEqual(f.commands,[])
    def test_failed_command_no_commit_or_extra_command(self):
        f=Fixture()
        def reject(*args,**kw):return {'error':'failure'}
        f.command=reject
        self.assertEqual(f.run()['state'],'failed')
        self.assertEqual(f.commits,[])
    def test_load_timeout_leaves_paused_no_commit(self):
        f=Fixture();command=f.command
        def not_loaded(*args,**kw):
            r=command(*args,**kw);f.state['loaded']=False;return r
        f.command=not_loaded
        self.assertEqual(f.run()['state'],'load_unverified')
        self.assertTrue(f.state['pause'])
        self.assertEqual(len(f.commands),1)
    def test_starvation_that_recovers_during_observation_never_loads(self):
        f=Fixture()
        def recovered(seconds):f.t+=seconds;f.state['paused_for_cache']=False
        f.sleep=recovered
        self.assertEqual(f.run()['state'],'no_longer_starved')
        self.assertEqual(f.commands,[])
    def test_non_hls_and_drm_sources_are_ineligible(self):
        for field,value in [('kind','youtube'),('drm',True),('url','file:///private')]:
            f=Fixture();f.source[field]=value
            self.assertEqual(f.run()['state'],'ineligible')


class PostloadPreservationTests(unittest.TestCase):
    def test_wrong_restore_position_never_resumes(self):
        f=Fixture();command=f.command
        def wrong(*a,**kw):
            r=command(*a,**kw);f.state['position']=0;return r
        f.command=wrong
        self.assertEqual(f.run()['state'],'preservation_unverified')
        self.assertEqual(len(f.commands),1)
        self.assertEqual(f.commits,[])
    def test_loaded_unpaused_never_accepted_as_staged(self):
        f=Fixture();command=f.command
        def unpaused(*a,**kw):
            r=command(*a,**kw);f.state['pause']=False;return r
        f.command=unpaused
        self.assertEqual(f.run()['state'],'preservation_unverified')
        self.assertEqual(len(f.commands),1)
    def test_subtitle_visibility_changed_during_resolve_cancels(self):
        f=Fixture();resolve=f.resolve
        def changed(*a,**kw):
            r=resolve(*a,**kw);f.state['sub_visibility']=True;return r
        f.resolve=changed
        self.assertEqual(f.run()['state'],'cancelled')
        self.assertEqual(f.commands,[])
    def test_every_retained_selection_is_fenced_before_resume(self):
        for key,value in [('sid',4),('aid',4),('sub_visibility',True),
                          ('season','3'),('episode','4'),('voice','de'),('mode','tv'),
                          ('session','new'),('endpoint','other')]:
            with self.subTest(key=key):
                f=Fixture();command=f.command
                def changed(*a,**kw):
                    r=command(*a,**kw);f.state[key]=value;return r
                f.command=changed
                self.assertEqual(f.run()['state'],'cancelled')
                self.assertEqual(len(f.commands),1)
                self.assertEqual(f.commits,[])
    def test_resolver_cannot_inject_mpv_options(self):
        f=Fixture();f.fresh['mpv_options']={'volume':'100','script':'untrusted'}
        self.assertEqual(f.run()['state'],'recovered')
        self.assertNotIn('volume',f.commands[0][-1])
        self.assertNotIn('script',f.commands[0][-1])
    def test_decoded_frames_without_clock_progress_not_success(self):
        f=Fixture()
        def frozen(seconds):
            f.t+=seconds
            if f.commands and not f.state['pause']:
                f.state['frames']+=30;f.state['paused_for_cache']=False
        f.sleep=frozen
        self.assertEqual(f.run()['state'],'progress_unverified')
        self.assertEqual(f.commits,[])
    def test_resolve_timeout_contract_and_exception_never_load(self):
        f=Fixture()
        def fail(*a,**kw):raise TimeoutError()
        f.resolve=fail
        self.assertEqual(f.run()['state'],'failed')
        self.assertEqual(f.commands,[])

class TransportTests(unittest.TestCase):
    def test_reviewed_adapter_can_preserve_required_transport_headers(self):
        f=Fixture()
        result=f.run(transport_options=lambda source:{'referrer':'https://example.invalid/episode'})
        self.assertEqual(result['state'],'recovered')
        self.assertEqual(f.commands[0][-1]['referrer'],'https://example.invalid/episode')
    def test_adapter_cannot_change_unrelated_playback_properties(self):
        f=Fixture()
        self.assertEqual(f.run(transport_options=lambda source:{'volume':100})['state'],'cancelled')
        self.assertEqual(f.commands,[])

if __name__=='__main__':
    unittest.main()
