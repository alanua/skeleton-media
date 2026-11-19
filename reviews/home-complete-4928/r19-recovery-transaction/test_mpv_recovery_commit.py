import copy
import json
import tempfile
import unittest
from pathlib import Path
from playback_intent import PlaybackIntent
from mpv_recovery_commit import RecoveryCommit, RecoveryCommitUnavailable
from test_postload_transaction import RecoveryFixture

class CommitFixture(RecoveryFixture):
    def __init__(self):
        super().__init__()
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.gate=PlaybackIntent()
        self.admission['revision']=0;self.rev=0
        self.current.update(content_key='owned-content')
        self.admission['current']=copy.deepcopy(self.current)
        # Re-capture session after adding canonical current metadata.
        self.admission=self.reader_object.capture()
        self.last={}
        self.writes=[];self.fail_at=None
        self.write_hook=lambda stage:None
    def save(self,name,value):
        self.writes.append(name)
        if self.fail_at==name:raise OSError('synthetic disk failure')
        target=self.root/(name+'.json');tmp=target.with_suffix('.tmp')
        tmp.write_text(json.dumps(value));tmp.chmod(0o600);tmp.replace(target)
        self.write_hook(name)
    def make_commit(self):
        for name,value in [('job',self.job),('current',self.current)]:self.save(name,value)
        self.writes.clear()
        read=lambda name:json.loads((self.root/(name+'.json')).read_text())
        def last(job,source,pos,paused):
            self.save('last',{'job_id':job['job_id'],'source_id':source['source_id'],
                             'position_seconds':pos,'paused':paused,'content_key':'owned-content'})
        return RecoveryCommit(
            admission=self.admission,read_job=lambda jid:read('job'),read_current=lambda:read('current'),
            write_job=lambda job:self.save('job',job),write_current=lambda current:self.save('current',current),
            conditional_commit=self.gate.commit,
            ownership=lambda:{'endpoint':'home_edge_01','mode':self.active,'process':self.proc,'revision':self.gate.read()},
            snapshot=lambda fresh:self.post.capture(self.admission,fresh),
            write_last_vod=last,read_last_vod=lambda:read('last'))
    def staged_state(self):
        self.command(['loadfile',self.fresh['url'],'replace',-1,{}],2,expected_revision=0)
        self.restore(self.fresh,0)
        self.values['pause']=False;self.values['paused-for-cache']=False
        self.values['time-pos']=45
    def consistent_pointer(self):
        job=json.loads((self.root/'job.json').read_text())
        current=json.loads((self.root/'current.json').read_text())
        return any(s['source_id']==current['source_id'] for s in job['sources'])
    def close(self):self.tmp.cleanup()

class DurableCommitTests(unittest.TestCase):
    def fixture(self):
        f=CommitFixture();self.addCleanup(f.close);c=f.make_commit();f.staged_state();return f,c
    def test_real_atomic_files_preserve_old_source_and_session_metadata(self):
        f,c=self.fixture();old=copy.deepcopy(f.admission['current'])
        result=c(f.job,f.fresh,expected_revision=0)
        current=json.loads((f.root/'current.json').read_text());job=json.loads((f.root/'job.json').read_text())
        self.assertTrue(result['committed']);self.assertEqual(f.writes,['job','current','last'])
        self.assertEqual(job['sources'][0],f.source)
        self.assertEqual(len(job['sources']),2)
        for key in ('pid','started_at','job_id','content_key'):
            self.assertEqual(current[key],old[key])
        self.assertEqual(current['source_snapshot']['url'],f.fresh['url'])
        self.assertEqual(current['resume_from_seconds'],45)
        self.assertEqual(current['recovery_selection']['aid'],7)
        self.assertTrue(f.consistent_pointer())
    def test_failure_at_each_publication_step_keeps_valid_source_pointer(self):
        for stage in ('job','current','last'):
            with self.subTest(stage=stage):
                f,c=self.fixture();f.fail_at=stage
                with self.assertRaises(OSError):c(f.job,f.fresh,expected_revision=0)
                self.assertTrue(f.consistent_pointer())
                self.assertEqual(json.loads((f.root/'job.json').read_text())['sources'][0],f.source)
    def test_acknowledged_but_unapplied_current_is_not_success(self):
        f,c=self.fixture();c.write_current=lambda current:None
        with self.assertRaises(RecoveryCommitUnavailable):c(f.job,f.fresh,expected_revision=0)
        self.assertTrue(f.consistent_pointer())
    def test_last_vod_wrong_or_missing_source_is_not_success(self):
        f,c=self.fixture();c.read_last_vod=lambda:{'source_id':'foreign'}
        with self.assertRaises(RecoveryCommitUnavailable):c(f.job,f.fresh,expected_revision=0)
        self.assertTrue(f.consistent_pointer())
    def test_new_operator_intent_prevents_all_durable_writes(self):
        f,c=self.fixture();f.gate.execute(['seek',10],lambda:None)
        with self.assertRaises(Exception):c(f.job,f.fresh,expected_revision=0)
        self.assertEqual(f.writes,[])
    def test_changed_job_current_process_mode_prevents_all_writes(self):
        for changed in ('job','current','process','mode'):
            f,c=self.fixture()
            if changed in ('job','current'):
                p=f.root/(changed+'.json');v=json.loads(p.read_text());v['edited']=True;p.write_text(json.dumps(v))
            elif changed=='process':f.proc=('boot',99,'101')
            else:f.active='kiosk'
            with self.assertRaises(RecoveryCommitUnavailable):c(f.job,f.fresh,expected_revision=0)
            self.assertEqual(f.writes,[])
    def test_conflicting_source_id_never_overwrites_unrelated_source(self):
        f,c=self.fixture()
        extra=dict(f.fresh,url='https://example.invalid/other')
        f.job['sources'].append(extra);f.admission['job']=copy.deepcopy(f.job);c=f.make_commit()
        with self.assertRaises(RecoveryCommitUnavailable):c(f.job,f.fresh,expected_revision=0)
        self.assertEqual(f.writes,[])
    def test_same_source_id_refresh_gets_new_additive_id(self):
        f,c=self.fixture();f.fresh['source_id']=f.source['source_id']
        result=c(f.job,f.fresh,expected_revision=0)
        self.assertNotEqual(result['source_id'],f.source['source_id'])
        self.assertTrue(f.consistent_pointer())
    def test_wrong_voice_or_episode_never_publishes(self):
        for key,value in [('group','de'),('episode','99')]:
            f,c=self.fixture();f.fresh[key]=value
            with self.assertRaises(RecoveryCommitUnavailable):c(f.job,f.fresh,expected_revision=0)
            self.assertEqual(f.writes,[])
    def test_job_change_after_staging_does_not_clobber_new_current(self):
        f,c=self.fixture()
        def changed(stage):
            if stage=='job':
                p=f.root/'current.json';value=json.loads(p.read_text());value['source_id']='operator-new';p.write_text(json.dumps(value))
        f.write_hook=changed
        with self.assertRaises(RecoveryCommitUnavailable):c(f.job,f.fresh,expected_revision=0)
        self.assertEqual(f.writes,['job'])
        self.assertEqual(json.loads((f.root/'current.json').read_text())['source_id'],'operator-new')

if __name__=='__main__':unittest.main()
