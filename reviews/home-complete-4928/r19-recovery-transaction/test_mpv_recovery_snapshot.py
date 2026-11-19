import copy
import unittest
from mpv_recovery_snapshot import MPVRecoverySnapshot, SnapshotUnavailable, selected_track_identity


class Source:
    def __init__(self):
        self.t=1.;self.rev=7;self.active='mpv';self.proc=('boot',42,'100')
        self.current={'pid':42,'started_at':50,'job_id':'a'*16,'source_id':'s'}
        self.source={'source_id':'s','kind':'hls','url':'https://example.invalid/old.m3u8','season':'2','episode':'3','group':'uk'}
        self.job={'job_id':'a'*16,'sources':[self.source]}
        self.values={'pause':False,'time-pos':42.,'paused-for-cache':True,'seeking':False,
            'path':self.source['url'],'stream-open-filename':self.source['url'],
            'vo-configured':True,'idle-active':False,'eof-reached':False,
            'sid':'no','aid':1,'sub-visibility':False,
            'track-list':[{'id':1,'type':'audio','lang':'uk','codec':'aac','selected':True}]}
        self.commands=[];self.hook=lambda prop:None
    def command(self,parts,timeout):
        self.commands.append(parts);self.hook(parts[1])
        return {'error':'success','data':copy.deepcopy(self.values[parts[1]])}
    def reader(self,frames=None):
        return MPVRecoverySnapshot(record=lambda:copy.deepcopy(self.current),
            job=lambda _:copy.deepcopy(self.job),mode=lambda:self.active,
            process=lambda:self.proc,revision=lambda:self.rev,command=self.command,
            season=lambda j,s:s['season'],episode=lambda j,s:s['episode'],
            frame_evidence=frames,clock=lambda:self.t)


class SnapshotTests(unittest.TestCase):
    def test_exact_current_job_and_cache_properties_without_mutations(self):
        f=Source();r=f.reader().capture()
        self.assertEqual(r['snapshot']['job_id'],'a'*16)
        self.assertEqual(r['snapshot']['paused_for_cache'],True)
        self.assertEqual(r['snapshot']['position'],42.)
        self.assertEqual(r['snapshot']['voice'],'uk')
        self.assertIsNone(r['snapshot']['frames'])
        self.assertTrue(all(c[0]=='get_property' for c in f.commands))
        r['job']['sources'].clear()
        self.assertEqual(len(f.job['sources']),1)
    def test_missing_or_foreign_frame_evidence_is_never_invented(self):
        for sample in [None,{}, {'process':('boot',99,'100'),'path':'x','observed_at':1,'frames':10},
                       {'process':('boot',42,'100'),'path':'https://example.invalid/old.m3u8','observed_at':0,'frames':10}]:
            f=Source()
            self.assertIsNone(f.reader(lambda:sample).capture()['snapshot']['frames'])
    def test_independent_counter_requires_exact_process_path_and_freshness(self):
        f=Source()
        sample={'process':f.proc,'path':f.source['url'],'observed_at':f.t,'frames':10}
        self.assertEqual(f.reader(lambda:sample).capture()['snapshot']['frames'],10)
    def test_changed_mode_revision_process_current_or_job_cancels_snapshot(self):
        for target in ['mode','revision','process','current','job']:
            with self.subTest(target=target):
                f=Source()
                def change(prop):
                    if prop!='track-list':return
                    if target=='mode':f.active='kiosk'
                    if target=='revision':f.rev+=1
                    if target=='process':f.proc=('boot',42,'101')
                    if target=='current':f.current['started_at']+=1
                    if target=='job':f.job['title']='edited elsewhere'
                f.hook=change
                with self.assertRaises(SnapshotUnavailable):f.reader().capture()
    def test_direct_user_selection_change_during_sample_is_detected(self):
        f=Source()
        def change(prop):
            if prop=='track-list':f.values['pause']=True
        f.hook=change
        with self.assertRaises(SnapshotUnavailable):f.reader().capture()
    def test_wrong_endpoint_or_mode_never_queries_player(self):
        for endpoint,mode in [('samsung','mpv'),('home_edge_01','games')]:
            f=Source();f.active=mode
            with self.assertRaises(SnapshotUnavailable):f.reader().capture(endpoint)
            self.assertEqual(f.commands,[])
    def test_duplicate_source_or_foreign_path_is_rejected(self):
        for variant in ['duplicate','path']:
            f=Source()
            if variant=='duplicate':f.job['sources'].append(copy.deepcopy(f.source))
            else:f.values['path']=f.values['stream-open-filename']='https://example.invalid/foreign'
            with self.assertRaises(SnapshotUnavailable):f.reader().capture()
    def test_partial_property_failure_never_returns_plausible_snapshot(self):
        f=Source()
        def fail(parts,timeout):return {'error':'property unavailable'}
        f.command=fail
        with self.assertRaises(SnapshotUnavailable):f.reader().capture()
    def test_selected_audio_requires_unique_confirmed_track(self):
        for tracks in [[],[{'id':1,'type':'audio','selected':False}],
            [{'id':1,'type':'audio','selected':True}]*2]:
            with self.assertRaises(SnapshotUnavailable):selected_track_identity(tracks,'audio',1)
        self.assertTrue(selected_track_identity([], 'sub','no')['disabled'])
    def test_snapshot_deadline_is_bounded(self):
        f=Source()
        f.hook=lambda prop:setattr(f,'t',f.t+.4)
        with self.assertRaises(SnapshotUnavailable):f.reader().capture()
        self.assertLessEqual(len(f.commands),8)


if __name__=='__main__':
    unittest.main()
