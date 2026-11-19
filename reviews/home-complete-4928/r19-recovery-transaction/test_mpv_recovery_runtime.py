"""Canonical adapter integration with real private files/PNG and synthetic IPC."""
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from PIL import Image

from mpv_recovery_runtime import MPVRecoveryRuntime, mode_guard, owned_mpv_process
from playback_intent import PlaybackIntent
from test_mpv_recovery_snapshot import Source


class AdapterFixture(Source):
    def __init__(self, root):
        super().__init__()
        self.root=Path(root);self.gate=PlaybackIntent();self.t=100.
        self.mutations=[];self.images=0;self.resolves=0;self.loaded=False
        self.pixel=0;self.after_mutation=lambda parts:None;self.after_resolve=lambda:None
        self.before_sleep=lambda seconds:None;self.fail_write=None;self.writes=[]
        self.pid_record=self.root/'current.json';self.jobs=self.root/'player-jobs'
        self.jobs.mkdir();self.app_jobs=self.root/'app-jobs';self.app_jobs.mkdir()
        self.job.update(title='Retained title',catalog={'media_uid':'tv:1'},
                        family={'retained':True},content_key='tv:1')
        self.current.update(content_key='tv:1',poster='retained-poster',translation='uk')
        self.fresh=dict(self.source,url='https://example.invalid/fresh.m3u8',source_id='fresh')
        self.save(self.pid_record,self.current)
        self.save(self.jobs/(self.job['job_id']+'.json'),dict(self.job,player_only='keep'))
        self.save(self.app_jobs/(self.job['job_id']+'.json'),self.job)
        self.mode_dir=self.root/'tv-mode';self.mode_dir.mkdir()
        self.mode_file=self.mode_dir/'current';self.mode_file.write_text('mpv')
        self.request=self.mode_dir/'requested'
        worker=self.root/'resolver-worker.py';worker.write_text('# owned test fixture')
        self.player=SimpleNamespace(
            CURRENT=self.pid_record,JOBS=self.jobs,LAST_VOD=self.root/'last-vod.json',
            MODE_FILE=self.mode_file,MODE_REQUEST_FILE=self.request,
            _mode_transition_target=lambda:None,
            _current_tv_mode=lambda:self.mode_file.read_text(),
            _PLAYBACK_INTENT=self.gate,playback_control_revision=self.gate.read,
            command=self.command,_command_wire=self.wire,
            _mpv_source_configuration=self.transport,_atomic_private=self.atomic,
            _write_last_vod=self.last_vod)
        self.app=SimpleNamespace(
            _job_path=lambda jid:self.app_jobs/(jid+'.json'),
            _job_write_path=lambda job:self.app_jobs/(job['job_id']+'.json'),
            _history_source_season=lambda j,s:str(s.get('season','')),
            _history_source_episode=lambda j,s:'Episode '+str(s['episode']).replace('Episode ',''),
            _refresh_stale_source=self.refresh)
        self.runtime=MPVRecoveryRuntime(app=self.app,player=self.player,
            resolver_worker=worker,root=self.root/'recovery',
            clock=lambda:self.t,sleep=self.sleep)
        self.runtime.process=lambda:self.proc
        self.runtime.resolver=self.resolver

    def save(self,path,value):
        temp=Path(str(path)+'.tmp')
        temp.write_text(json.dumps(value));temp.chmod(0o600);temp.replace(path)
    def read(self,path):return json.loads(path.read_text())
    def atomic(self,path,value):
        self.writes.append(Path(path).name)
        if Path(path).name==self.fail_write:raise OSError('fixture write failure')
        self.save(path,value)
    def last_vod(self,job,source,position,duration,paused,reason):
        self.atomic(self.player.LAST_VOD,dict(job_id=job['job_id'],
            source_id=source['source_id'],content_key='tv:1',
            position_seconds=position,paused=paused))
    def resolver(self,origin,timeout):
        self.resolves+=1;return {'sources':[copy.deepcopy(self.fresh)]}
    def refresh(self,job,source,*,persist,resolver):
        assert persist is False
        result=resolver('https://example.invalid/owned-origin')
        fresh=copy.deepcopy(result['sources'][0])
        job['sources']=[fresh]
        self.after_resolve()
        return fresh
    def transport(self,job,source,subtitles):
        return source['url'],[],{'user-agent':'fixture','referrer':'https://example.invalid/',
                                'http-header-fields':'Origin: https://example.invalid','sid':'no'}
    def command(self,parts,timeout,expected_revision=None):
        return self.gate.execute(parts,lambda:self.wire(parts,timeout),
                                 expected_revision=expected_revision)
    def wire(self,parts,timeout):
        if parts[0]=='get_property':
            value=90. if parts[1]=='duration' else self.values[parts[1]]
            return {'error':'success','data':copy.deepcopy(value)}
        if parts[0]=='screenshot-to-file':
            self.images+=1;Image.new('RGB',(4,4),(self.pixel,0,0)).save(parts[1])
            return {'error':'success'}
        self.mutations.append(copy.deepcopy(parts))
        if parts[0]=='loadfile':
            self.loaded=True
            self.values.update(path=parts[1],**{'stream-open-filename':parts[1],
                'pause':True,'track-list':[
                    {'id':1,'type':'audio','lang':'de','codec':'aac','selected':True},
                    {'id':7,'type':'audio','lang':'uk','codec':'aac','selected':False}]})
        elif parts[0]=='set_property':
            _,prop,value=parts;self.values[prop]=value
            if prop in ('aid','sid'):
                for track in self.values['track-list']:
                    if track['type']==('audio' if prop=='aid' else 'sub'):
                        track['selected']=track['id']==value
        self.after_mutation(parts)
        return {'error':'success'}
    def sleep(self,seconds):
        self.before_sleep(seconds);self.t+=seconds
        if self.loaded and self.values['pause'] is False:
            self.values['time-pos']+=seconds
            self.pixel+=1;self.values['paused-for-cache']=False
    def run(self):return self.runtime.run_once()


class CanonicalAdapterTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        return AdapterFixture(temp.name)
    def test_whole_adapter_preserves_canonical_selection_and_both_real_job_files(self):
        f=self.fixture();before=copy.deepcopy(f.current);result=f.run()
        self.assertEqual(result['state'],'recovered',result)
        current=f.read(f.pid_record)
        for key in ('pid','started_at','job_id','content_key','poster','translation'):
            self.assertEqual(current[key],before[key])
        self.assertEqual(current['source_id'],'fresh')
        self.assertEqual(current['recovery_selection']['aid'],7)
        self.assertEqual(current['recovery_selection']['sid'],'no')
        for directory in (f.jobs,f.app_jobs):
            job=f.read(directory/(f.job['job_id']+'.json'))
            self.assertEqual([s['source_id'] for s in job['sources']],['s','fresh'])
            self.assertEqual(job['family'],{'retained':True})
        self.assertEqual(f.read(f.jobs/(f.job['job_id']+'.json'))['player_only'],'keep')
        self.assertEqual(f.gate.read(),0) # screenshot observation MUST NOT invalidate
        self.assertGreater(f.images,0)
        self.assertFalse(list((f.root/'recovery/frames').rglob('*.png')))
        self.assertEqual(f.resolves,1)
        self.assertEqual(f.mutations[0][4]['pause'],'yes')
        self.assertNotIn('sid',f.runtime._transport(f.job,f.fresh))
    def test_user_pause_during_resolve_prevents_load(self):
        f=self.fixture()
        f.after_resolve=lambda:f.command(['set_property','pause',True],2)
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertFalse(any(p[0]=='loadfile' for p in f.mutations))
        self.assertEqual(f.writes,[])
    def test_same_value_user_pause_after_load_prevents_resume(self):
        f=self.fixture()
        def user(parts):
            if parts[0]=='loadfile':f.gate.operation(lambda:None)()
        f.after_mutation=user
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertTrue(f.values['pause']);self.assertEqual(len(f.mutations),1)
        self.assertEqual(f.writes,[])
    def test_requested_mode_before_load_prevents_all_media_mutation(self):
        f=self.fixture();f.after_resolve=lambda:f.request.write_text('kiosk')
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertEqual(f.mutations,[]);self.assertEqual(f.writes,[])
    def test_existing_mode_flock_prevents_load_without_wait(self):
        f=self.fixture()
        with mode_guard(f.mode_dir/'lock'):
            result=f.run()
        self.assertNotEqual(result['state'],'recovered');self.assertEqual(f.mutations,[])
    def test_changed_player_job_during_resolution_is_never_overwritten(self):
        f=self.fixture()
        def change():
            p=f.jobs/(f.job['job_id']+'.json');job=f.read(p)
            job['new_user_selection']='keep';f.save(p,job)
        f.after_resolve=change
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertEqual(f.writes,[])
    def test_dual_job_write_failure_leaves_old_current_source_valid(self):
        f=self.fixture()
        original=f.atomic
        def fail_player(path,value):
            if Path(path).parent==f.jobs:raise OSError('fixture')
            return original(path,value)
        f.player._atomic_private=fail_player
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertEqual(f.read(f.pid_record)['source_id'],'s')
        self.assertIn('s',[s['source_id'] for s in f.read(f.jobs/(f.job['job_id']+'.json'))['sources']])
    def test_missing_real_image_evidence_cannot_commit(self):
        f=self.fixture();f.runtime._image_command=lambda *a,**k:{'error':'unavailable'}
        self.assertEqual(f.run()['state'],'progress_unverified')
        self.assertEqual(f.writes,[])
    def test_durable_retry_bound_survives_runtime_reconstruction(self):
        f=self.fixture();f.runtime.resolver=lambda *a,**kw:(_ for _ in ()).throw(TimeoutError())
        self.assertEqual(f.run()['state'],'failed')
        self.assertEqual(f.run()['state'],'cooldown')
        f.t+=61;f.runtime.engine=__import__('hls_recovery_transaction').HLSRecovery()
        self.assertEqual(f.run()['state'],'failed')
        f.t+=61;self.assertEqual(f.run()['state'],'retry_exhausted')
        self.assertEqual(f.mutations,[])
    def test_paused_or_healthy_playback_never_resolves(self):
        for field,value in [('pause',True),('paused-for-cache',False)]:
            f=self.fixture();f.values[field]=value
            self.assertEqual(f.run()['state'],'ineligible')
            self.assertEqual(f.resolves,0);self.assertEqual(f.mutations,[])
    def test_image_transport_rejects_arbitrary_commands_and_foreign_files(self):
        f=self.fixture()
        for parts in [['set_property','pause',False],['screenshot-to-file','/tmp/decoded.png','video']]:
            with self.assertRaises(Exception):f.runtime._image_command(parts,timeout=.8)
        self.assertEqual(f.mutations,[])
    def test_async_load_properties_are_waited_for_without_faking_a_snapshot(self):
        f=self.fixture();original=f.wire;pending=[2]
        def wire(parts,timeout):
            if f.loaded and parts==['get_property','time-pos'] and pending[0]:
                pending[0]-=1;return {'error':'property unavailable'}
            return original(parts,timeout)
        f.player._command_wire=wire
        f.player.command=lambda parts,timeout,expected_revision=None:f.gate.execute(
            parts,lambda:wire(parts,timeout),expected_revision=expected_revision)
        self.assertEqual(f.run()['state'],'recovered')
        self.assertEqual(pending[0],0)
    def test_missing_properties_expire_load_window_and_never_resume(self):
        f=self.fixture();original=f.wire
        def wire(parts,timeout):
            if f.loaded and parts==['get_property','time-pos']:
                return {'error':'property unavailable'}
            return original(parts,timeout)
        f.player.command=lambda parts,timeout,expected_revision=None:f.gate.execute(
            parts,lambda:wire(parts,timeout),expected_revision=expected_revision)
        self.assertEqual(f.run()['state'],'load_unverified')
        self.assertTrue(f.values['pause']);self.assertEqual(f.writes,[])
        self.assertEqual(len(f.mutations),1)
    def test_new_user_intent_during_pending_load_prevents_resume(self):
        f=self.fixture();original=f.wire
        def wire(parts,timeout):
            if f.loaded and parts==['get_property','time-pos']:
                return {'error':'property unavailable'}
            return original(parts,timeout)
        f.player.command=lambda parts,timeout,expected_revision=None:f.gate.execute(
            parts,lambda:wire(parts,timeout),expected_revision=expected_revision)
        def user(seconds):
            if f.loaded:f.gate.operation(lambda:None)()
        f.before_sleep=user
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertTrue(f.values['pause']);self.assertEqual(f.writes,[])

    def test_real_mpv_disabled_boolean_tracks_normalize_to_no(self):
        f=self.fixture();f.values['aid']=False;f.values['sid']=False
        f.values['track-list']=[]
        original=f.wire
        def wire(parts,timeout):
            reply=original(parts,timeout)
            if parts==['get_property','aid'] and reply.get('data')=='no':reply['data']=False
            if parts==['get_property','sid'] and reply.get('data')=='no':reply['data']=False
            return reply
        f.player.command=lambda parts,timeout,expected_revision=None:f.gate.execute(
            parts,lambda:wire(parts,timeout),expected_revision=expected_revision)
        result=f.run();self.assertEqual(result['state'],'recovered',result)
        self.assertEqual(f.mutations[0][4]['aid'],'no')
        self.assertEqual(f.read(f.pid_record)['recovery_selection']['aid'],'no')

    def test_total_deadline_prevents_late_resolve_or_mutation(self):
        f=self.fixture();f.before_sleep=lambda seconds:setattr(f,'t',f.t+46)
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertEqual(f.resolves,0);self.assertEqual(f.mutations,[])

    def test_private_error_details_do_not_escape(self):
        f=self.fixture();f.runtime.process=lambda:(_ for _ in ()).throw(RuntimeError('private-token'))
        self.assertEqual(f.run(),{'state':'adapter_unavailable','loaded':False})


if __name__=='__main__':unittest.main()
