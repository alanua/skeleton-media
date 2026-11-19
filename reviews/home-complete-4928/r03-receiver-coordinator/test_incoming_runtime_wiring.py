import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from patch_incoming_runtime_wiring import patch_app,APP_HELPER

ROOT=Path(__file__).parent
class StopMonitor(BaseException):pass

class ExistingMonitorWiringTests(unittest.TestCase):
    def monitor(self,incoming):
        source=(ROOT/'work/backend/app.py').read_text()
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef)
                  and n.name=='_video_autoplay_poll_monitor')
        events=[]
        ns=dict(_incoming_youtube_tick=incoming,
            time=SimpleNamespace(time=lambda:100,sleep=lambda _:(_ for _ in ()).throw(StopMonitor())),
            json=SimpleNamespace(loads=lambda _:{}),
            player=SimpleNamespace(CURRENT=SimpleNamespace(read_text=lambda **kw:'{}')))
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual-existing-monitor>','exec'),ns)
        return ns
    def test_receiver_tick_runs_even_without_active_vod_or_autoplay(self):
        events=[];ns=self.monitor(lambda:events.append('incoming'))
        with self.assertRaises(StopMonitor):ns['_video_autoplay_poll_monitor']()
        self.assertEqual(events,['incoming'])
    def test_receiver_callback_failure_does_not_stop_existing_poll(self):
        events=[]
        def fail():events.append('incoming');raise OSError('private')
        ns=self.monitor(fail)
        with self.assertRaises(StopMonitor):ns['_video_autoplay_poll_monitor']()
        self.assertEqual(events,['incoming'])
    def helper(self,result):
        now=[100.];calls=[];writes=[]
        runtime=SimpleNamespace(root=Path('/fixture/private'),tick=lambda:(calls.append(1) or result))
        ns=dict(time=SimpleNamespace(monotonic=lambda:now[0],time=lambda:1000),
                player=SimpleNamespace(_atomic_private=lambda p,v:writes.append(v)))
        exec(APP_HELPER,ns);ns['_INCOMING_YOUTUBE_RUNTIME']=runtime
        return ns,now,calls,writes
    def test_repeated_result_does_not_write_every_poll(self):
        ns,now,calls,writes=self.helper({'state':'no_fresh_load'})
        ns['_incoming_youtube_tick']();now[0]+=1;ns['_incoming_youtube_tick']()
        self.assertEqual(len(calls),2);self.assertEqual(len(writes),1)
    def test_failed_observer_has_bounded_backoff_without_recreating_a_worker(self):
        ns,now,calls,writes=self.helper({'state':'observer_unavailable','error_type':'RuntimeError'})
        ns['_incoming_youtube_tick']();self.assertEqual(ns['_INCOMING_YOUTUBE_NEXT_RETRY'],102)
        now[0]=101;ns['_incoming_youtube_tick']();self.assertEqual(len(calls),1)
        now[0]=103;ns['_incoming_youtube_tick']();self.assertEqual(len(calls),2)
        self.assertEqual(ns['_INCOMING_YOUTUBE_NEXT_RETRY'],107)
    def test_private_error_payload_fields_are_not_published(self):
        ns,now,calls,writes=self.helper({'state':'failed','signed_url':'private','token':'private'})
        ns['_incoming_youtube_tick']()
        self.assertEqual(writes[0],dict(schema='skeleton.incoming-youtube-result.v1',
            checked_at=1000,state='failed'))
    def test_permission_denial_halts_retries_until_conditions_change(self):
        ns,now,calls,writes=self.helper({'state':'observer_unavailable','error_type':'PermissionError','error_site':'fixture:read:1'})
        ns['_incoming_youtube_tick']();now[0]+=300;ns['_incoming_youtube_tick']()
        self.assertEqual(len(calls),1)
        self.assertTrue(writes[0]['retry_blocked'])
        self.assertEqual(writes[0]['error_site'],'fixture:read:1')

    def test_runtime_exception_has_retry_backoff_and_does_not_escape(self):
        ns,now,calls,writes=self.helper({})
        ns['_INCOMING_YOUTUBE_RUNTIME'].tick=lambda:(_ for _ in ()).throw(RuntimeError('private'))
        ns['_incoming_youtube_tick']()
        self.assertEqual(ns['_INCOMING_YOUTUBE_NEXT_RETRY'],105);self.assertEqual(writes,[])
    def test_patch_preserves_activated_hls_helper_and_rejects_drift(self):
        before=(ROOT/'app.before-r03-wiring.py').read_text()
        after=patch_app(before)
        def fn(source,name):
            return ast.dump(next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name==name))
        for name in ('_hls_recovery_tick','_refresh_stale_source'):
            self.assertEqual(fn(before,name),fn(after,name))
        with self.assertRaises(ValueError):patch_app(after)
        with self.assertRaises(ValueError):patch_app(before+'\n')
    def test_new_receiver_helper_creates_no_thread_timer_or_executor(self):
        self.assertNotIn('threading.',APP_HELPER);self.assertNotIn('subprocess.',APP_HELPER)
        self.assertNotIn('Thread(',APP_HELPER)

if __name__=='__main__':unittest.main()
