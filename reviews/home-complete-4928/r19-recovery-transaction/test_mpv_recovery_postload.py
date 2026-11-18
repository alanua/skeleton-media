import copy
import unittest
from contextlib import nullcontext
from test_mpv_recovery_snapshot import Source
from mpv_recovery_snapshot import SnapshotUnavailable
from mpv_recovery_postload import MPVPostloadSnapshot, remap_track

class Fixture(Source):
    def __init__(self):
        super().__init__()
        self.admission = self.reader().capture()
        self.fresh = dict(self.source, url='https://example.invalid/fresh.m3u8', source_id='fresh')
        self.values.update(path=self.fresh['url'], **{'stream-open-filename':self.fresh['url'], 'pause':True})
        self.values['track-list'] = [
            {'id':1,'type':'audio','lang':'de','codec':'aac','selected':True},
            {'id':2,'type':'audio','lang':'uk','codec':'aac','selected':False},
        ]
        self.values['aid'] = 1
        self.mutations = []
        self.on_mutate = lambda: None
        self.adapter = MPVPostloadSnapshot(self.reader())
    def capture(self):
        return self.adapter.capture(self.admission, self.fresh)
    def mutate(self, parts, timeout, expected_revision):
        self.assert_revision = expected_revision
        self.mutations.append(parts)
        _, prop, value = parts
        self.values[prop] = value
        if prop in ('aid','sid'):
            kind = 'audio' if prop == 'aid' else 'sub'
            for t in self.values['track-list']:
                if t['type'] == kind:t['selected'] = t['id'] == value
        self.on_mutate()
        return {'error':'success'}
    def restore(self):
        return self.adapter.restore_tracks(
            self.admission,self.fresh,tracks=lambda:copy.deepcopy(self.values['track-list']),
            command=self.mutate,mutation_guard=nullcontext)

class PostloadTests(unittest.TestCase):
    def test_fresh_path_with_original_records_and_session(self):
        f=Fixture(); original=copy.deepcopy(f.job);r=f.capture()
        self.assertEqual(r['snapshot']['path'],f.fresh['url'])
        self.assertEqual(r['snapshot']['session'],f.admission['snapshot']['session'])
        self.assertEqual(r['job'],original)
        self.assertEqual(f.job,original)
        self.assertEqual(r['source']['url'],f.source['url'])
        self.assertIsNone(r['snapshot']['frames'])
    def test_wrong_path_rejected_before_restoration(self):
        f=Fixture();f.values['path']=f.values['stream-open-filename']='https://example.invalid/foreign'
        with self.assertRaises(SnapshotUnavailable):f.restore()
        self.assertEqual(f.mutations,[])
    def test_changed_job_record_process_mode_revision_rejected(self):
        for field in ('job','current','proc','active','rev'):
            with self.subTest(field=field):
                f=Fixture()
                if field=='job':f.job['edited']=True
                elif field=='current':f.current['started_at']+=1
                elif field=='proc':f.proc=('boot',42,'101')
                elif field=='active':f.active='kiosk'
                else:f.rev+=1
                with self.assertRaises(SnapshotUnavailable):f.restore()
                self.assertEqual(f.mutations,[])
    def test_new_url_wrong_voice_season_episode_live_or_drm_rejected(self):
        for key,value in [('group','de'),('season','99'),('episode','99'),('live',True),('drm',True),('is_trailer',True)]:
            f=Fixture();f.fresh[key]=value
            with self.assertRaises(SnapshotUnavailable):f.restore()
            self.assertEqual(f.mutations,[])
    def test_audio_id_reassigned_restores_language_before_any_resume(self):
        f=Fixture();r=f.restore()
        self.assertEqual(f.mutations,[['set_property','aid',2]])
        self.assertEqual(r['snapshot']['aid'],2)
        self.assertTrue(r['snapshot']['pause'])
        self.assertEqual(r['audio_identity']['lang'],'uk')
        self.assertEqual(f.assert_revision,f.rev)
    def test_no_ambiguous_same_language_guess(self):
        f=Fixture();f.values['track-list'].append({'id':3,'type':'audio','lang':'uk','codec':'aac','selected':False})
        with self.assertRaises(SnapshotUnavailable):f.restore()
        self.assertEqual(f.mutations,[])
    def test_missing_voice_never_silently_selects_other(self):
        f=Fixture();f.values['track-list']=f.values['track-list'][:1]
        with self.assertRaises(SnapshotUnavailable):f.restore()
        self.assertEqual(f.mutations,[])
    def test_subtitle_disabled_and_visibility_preserved(self):
        f=Fixture();f.values['sid']=4
        f.values['track-list'].append({'id':4,'type':'sub','lang':'uk','codec':'ass','selected':True})
        r=f.restore()
        self.assertEqual(r['snapshot']['sid'],'no')
        self.assertFalse(r['snapshot']['sub_visibility'])
        self.assertTrue(all(c[1] in ('aid','sid','sub-visibility') for c in f.mutations))
    def test_selected_external_subtitle_origin_cannot_be_replaced(self):
        identity={'type':'sub','lang':'uk','codec':'ass','external':True,'external-filename':'https://example.invalid/original.ass'}
        tracks=[dict(identity,id=7,**{'external-filename':'https://example.invalid/foreign.ass'})]
        with self.assertRaises(SnapshotUnavailable):remap_track(identity,tracks)
        self.assertEqual(remap_track(identity,[dict(identity,id=9)]),9)
    def test_demux_ids_can_change_without_switching_content(self):
        identity={'type':'audio','lang':'uk','codec':'aac','demux-id':0}
        self.assertEqual(remap_track(identity,[dict(identity,id=4,**{'demux-id':10})]),4)
    def test_seek_pause_change_or_revision_after_first_command_cancels(self):
        for field,value in [('time-pos',90),('pause',False),('revision',8)]:
            f=Fixture()
            def changed():
                if field=='revision':f.rev=value
                else:f.values[field]=value
            f.on_mutate=changed
            with self.assertRaises(SnapshotUnavailable):f.restore()
            self.assertEqual(len(f.mutations),1)
    def test_command_acknowledgement_without_application_is_rejected(self):
        f=Fixture();f.mutate=lambda *a,**kw:{'error':'success'}
        with self.assertRaises(SnapshotUnavailable):f.restore()
    def test_existing_admission_still_rejects_a_fresh_uncommitted_path(self):
        f=Fixture()
        with self.assertRaises(SnapshotUnavailable):f.reader().capture()

if __name__=='__main__':unittest.main()
