import copy
import unittest
from contextlib import nullcontext
from hls_recovery_transaction import HLSRecovery
from mpv_recovery_postload import MPVPostloadSnapshot
from test_mpv_recovery_snapshot import Source

class RecoveryFixture(Source):
    def __init__(self):
        super().__init__()
        self.t=100.
        self.frames=10
        self.mutations=[];self.commits=[];self.loaded=False
        self.fresh=dict(self.source,url='https://example.invalid/fresh.m3u8',source_id='fresh')
        self.after_mutation=lambda parts:None
        self.new_tracks=[
            {'id':1,'type':'audio','lang':'de','codec':'aac','selected':True},
            {'id':7,'type':'audio','lang':'uk','codec':'aac','selected':False},
        ]
        self.frame_available=True
        self.reader_object=self.reader(self.frame_sample)
        self.admission=self.reader_object.capture()
        self.post=MPVPostloadSnapshot(self.reader_object)
    def frame_sample(self):
        return {'process':self.proc,'path':self.values['path'],'observed_at':self.t,'frames':self.frames} if self.frame_available else None
    def command(self,parts,timeout,expected_revision=None):
        if parts[0]=='get_property':return super().command(parts,timeout)
        assert expected_revision==self.rev
        self.mutations.append(copy.deepcopy(parts))
        if parts[0]=='loadfile':
            self.loaded=True
            self.values.update(path=parts[1],**{'stream-open-filename':parts[1],'pause':True,'track-list':copy.deepcopy(self.new_tracks)})
        elif parts[0]=='set_property':
            _,prop,value=parts;self.values[prop]=value
            if prop in ('aid','sid'):
                kind='audio' if prop=='aid' else 'sub'
                for track in self.values['track-list']:
                    if track['type']==kind:track['selected']=track['id']==value
        self.after_mutation(parts)
        return {'error':'success'}
    def snapshot(self):
        return (self.post.capture(self.admission,self.fresh) if self.loaded
                else self.reader_object.capture())['snapshot']
    def sleep(self,seconds):
        self.t+=seconds
        if self.loaded and self.values['pause'] is False:
            self.values['time-pos']+=seconds;self.frames+=40
            self.values['paused-for-cache']=False
    def resolve(self,job,source,timeout):
        assert timeout==12
        return copy.deepcopy(self.fresh)
    def restore(self,fresh,expected_revision):
        assert expected_revision==self.rev
        return self.post.restore_tracks(
            self.admission,fresh,tracks=lambda:copy.deepcopy(self.values['track-list']),
            command=self.command,mutation_guard=nullcontext)['snapshot']
    def commit(self,job,source,expected_revision):
        assert expected_revision==self.rev
        self.commits.append((copy.deepcopy(job),copy.deepcopy(source)))
    def run(self):
        return HLSRecovery().run(
            job=self.job,source=self.source,snapshot=self.snapshot,revision=lambda:self.rev,
            resolve=self.resolve,command=self.command,mutation_guard=nullcontext,
            commit=self.commit,clock=lambda:self.t,sleep=self.sleep,restore_tracks=self.restore)

class IntegratedPostloadTests(unittest.TestCase):
    def test_actual_reader_and_restoration_complete_transaction(self):
        f=RecoveryFixture();old=copy.deepcopy(f.job);result=f.run()
        self.assertEqual(result['state'],'recovered')
        self.assertEqual([p[:2] for p in f.mutations],
                         [['loadfile',f.fresh['url']],['set_property','aid'],['set_property','pause']])
        self.assertEqual(f.values['aid'],7)
        self.assertEqual(f.job,old)
        self.assertEqual(f.commits[0][1]['source_id'],'fresh')
    def test_ambiguous_tracks_never_resume_or_commit(self):
        f=RecoveryFixture();f.new_tracks.append(dict(f.new_tracks[1],id=8))
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertEqual(len(f.mutations),1)
        self.assertTrue(f.values['pause']);self.assertEqual(f.commits,[])
    def test_intent_revision_during_restore_prevents_resume(self):
        f=RecoveryFixture()
        def change(parts):
            if parts[:2]==['set_property','aid']:f.rev+=1
        f.after_mutation=change
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertEqual(len(f.mutations),2)
        self.assertTrue(f.values['pause']);self.assertEqual(f.commits,[])
    def test_mode_change_during_load_prevents_track_or_pause_mutation(self):
        f=RecoveryFixture();f.after_mutation=lambda parts:setattr(f,'active','kiosk')
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertEqual(len(f.mutations),1);self.assertEqual(f.commits,[])
    def test_missing_actual_frame_evidence_never_commits_success(self):
        f=RecoveryFixture();f.frame_available=False
        self.assertEqual(f.run()['state'],'progress_unverified')
        self.assertEqual(f.commits,[])
    def test_seek_during_track_restore_never_resumes(self):
        f=RecoveryFixture()
        def change(parts):
            if parts[:2]==['set_property','aid']:f.values['time-pos']=90
        f.after_mutation=change
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertTrue(f.values['pause']);self.assertEqual(f.commits,[])
    def test_changed_durable_record_during_load_is_not_hidden_by_read_view(self):
        f=RecoveryFixture()
        f.after_mutation=lambda parts:f.current.update(started_at=999)
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertEqual(len(f.mutations),1);self.assertEqual(f.commits,[])
    def test_postload_wrong_pause_rejected_before_restoration(self):
        f=RecoveryFixture()
        def change(parts):
            if parts[0]=='loadfile':f.values['pause']=False
        f.after_mutation=change
        self.assertNotEqual(f.run()['state'],'recovered')
        self.assertEqual(len(f.mutations),1);self.assertEqual(f.commits,[])

if __name__=='__main__':unittest.main()
