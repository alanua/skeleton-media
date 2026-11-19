
import copy
import unittest
from mpv_recovery_snapshot import SnapshotUnavailable
from mpv_recovery_commit import RecoveryCommitUnavailable
from test_mpv_recovery_snapshot import Source
from test_mpv_recovery_commit import CommitFixture

class SessionPublicationTests(unittest.TestCase):
    def test_own_refresh_does_not_reset_the_session_retry_budget(self):
        f=Source();first=f.reader().capture()
        f.source=dict(f.source,source_id='new',url='https://example.invalid/new.m3u8')
        f.job['sources']=[f.source]
        f.current.update(source_id='new',source_snapshot=copy.deepcopy(f.source),resume_from_seconds=45)
        f.values.update(path=f.source['url'],**{'stream-open-filename':f.source['url']})
        second=f.reader().capture()
        self.assertEqual(first['snapshot']['session'],second['snapshot']['session'])
        self.assertNotEqual(first['current_sha256'],second['current_sha256'])
    def test_true_process_or_start_session_change_changes_budget_identity(self):
        for field in ('process','started_at','job_id'):
            f=Source();first=f.reader().capture()
            if field=='process':f.proc=('boot',42,'101')
            elif field=='started_at':f.current['started_at']+=1
            else:
                f.current['job_id']='b'*16;f.job['job_id']='b'*16
            second=f.reader().capture()
            self.assertNotEqual(first['snapshot']['session'],second['snapshot']['session'])
    def test_disabled_subtitle_cannot_be_silently_enabled_at_commit(self):
        f=CommitFixture();self.addCleanup(f.close);commit=f.make_commit();f.staged_state()
        f.values['sid']=8
        f.values['track-list'].append({'id':8,'type':'sub','lang':'uk','codec':'ass','selected':True})
        with self.assertRaises(RecoveryCommitUnavailable):
            commit(f.job,f.fresh,expected_revision=0)
        self.assertEqual(f.writes,[])

if __name__=='__main__':unittest.main()
