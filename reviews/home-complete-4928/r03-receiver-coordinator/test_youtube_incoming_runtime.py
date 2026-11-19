"""Actual runtime adapter with isolated native-read fixtures and owned controller."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from playback_intent import PlaybackIntent
from youtube_incoming_runtime import IncomingYouTubeRuntime
from test_youtube_lounge_xhr_observer import URL,snapshot,load
from test_youtube_lounge_events import frame

class Reader:
    def __init__(self,fixture):
        self.fixture=fixture;self.target_id='target';self.event_handler=None
        self.closed=False;self.enabled=False
    def enable_network_observation(self):self.enabled=True
    def snapshot(self):
        self.fixture.on_read()
        return copy.deepcopy(self.fixture.snapshots)
    def close(self):self.closed=True

class Fixture:
    def __init__(self,root):
        self.root=Path(root);self.now=100.;self.process=('boot',42,'100')
        self.mode=self.root/'current';self.mode.write_text('games\n')
        self.calls=self.root/'controller.json';self.published=[];self.on_read=lambda:None
        self.video='AAAAAAAAAAA';self.status_pause=False
        self.snapshots=[snapshot(frame([load(1)]))]
        self.gate=PlaybackIntent();self.target='target'
        controller=self.root/'controller'
        controller.write_text('#!/usr/bin/python3\nimport json,pathlib,sys\n'
            'pathlib.Path('+repr(str(self.calls))+').write_text(json.dumps(sys.argv[1:]))\n'
            'pathlib.Path('+repr(str(self.mode))+').write_text("kiosk\\n")\n')
        controller.chmod(0o700)
        self.player=SimpleNamespace(MODE_FILE=self.mode,MODE_REQUEST_FILE=self.root/'requested',
            _mode_transition_target=lambda:None,_current_tv_mode=lambda:self.mode.read_text().strip(),
            playback_control_revision=self.gate.read,_PLAYBACK_INTENT=self.gate,
            TV_MODE=str(controller),SOCKET=self.root/'absent.sock',
            _named_process_running=lambda _:False,
            mode_status=lambda:{'mode':self.mode.read_text().strip()})
        self.chrome={'http_json':lambda _:[{'type':'page','url':'https://www.youtube.com/tv','id':self.target}],
            'status':lambda:dict(video_id=self.video,running=True,pause=self.status_pause,player_state=1),
            '_youtube_windows':lambda _:['100'],'window_state':lambda:{'window_state':'fullscreen'},
            '_x11_env':lambda:{}}
        self.app=SimpleNamespace(media_state=SimpleNamespace(publish=lambda **kwargs:self.published.append(kwargs)))
        self.readers=[]
        def factory():
            reader=Reader(self);reader.target_id=self.target;self.readers.append(reader);return reader
        self.runtime=IncomingYouTubeRuntime(app=self.app,player=self.player,
            root=self.root/'state',actuate=True,clock=lambda:self.now,
            reader_factory=factory,chrome=self.chrome)
        self.runtime._process=lambda:self.process
    def tool_read(self,argv,**kwargs):
        if argv==['/usr/bin/xdotool','getactivewindow']:
            return SimpleNamespace(returncode=0,stdout='100\n')
        if argv==['/usr/bin/xdotool','getwindowpid','100']:
            return SimpleNamespace(returncode=0,stdout='42\n')
        raise AssertionError('Unexpected runtime command '+repr(argv))
    def tick(self):
        with patch('youtube_incoming_runtime.subprocess.run',side_effect=self.tool_read):
            return self.runtime.tick()
    def fresh(self,sequence=2,video='BBBBBBBBBBB'):
        self.now+=1;self.video=video
        prior=self.snapshots[0]['text'].encode()
        self.snapshots=[snapshot(prior+frame([load(sequence,video)]))]

class RuntimeAdapterTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        return Fixture(temp.name)
    def test_actual_adapter_observes_fresh_append_runs_owned_controller_and_publishes_directly(self):
        f=self.fixture();self.assertEqual(f.tick()['state'],'no_fresh_load')
        f.fresh();result=f.tick();self.assertEqual(result['state'],'youtube_verified',result)
        args=json.loads(f.calls.read_text())
        self.assertEqual(args[0],'kiosk-incoming')
        self.assertEqual(args[1],hashlib.sha256(f.video.encode()).hexdigest())
        self.assertEqual(len(args[2]),64)
        self.assertEqual(f.gate.read(),1)
        self.assertEqual(f.published[0]['mode']['mode'],'kiosk')
        self.assertEqual(f.published[0]['mode']['endpoint_id'],'home_edge_01')
        self.assertNotIn('volume',f.published[0])
    def test_bootstrap_multiple_old_requests_never_calls_controller(self):
        f=self.fixture()
        f.snapshots.append(snapshot(frame([load(2,'BBBBBBBBBBB')]),URL+'&zx=other'))
        self.assertEqual(f.tick()['state'],'no_fresh_load')
        self.assertFalse(f.calls.exists());self.assertEqual(f.published,[])
    def test_user_mode_change_between_snapshots_reprimes_instead_of_replaying_ambiguous_cast(self):
        f=self.fixture();f.tick();f.mode.write_text('mpv\n');f.fresh()
        self.assertEqual(f.tick()['state'],'no_fresh_load')
        self.assertFalse(f.calls.exists())
        f.fresh(3);self.assertEqual(f.tick()['state'],'youtube_verified')
    def test_control_revision_change_between_snapshots_reprimes(self):
        f=self.fixture();f.tick();f.gate.operation(lambda:None)();f.fresh()
        self.assertEqual(f.tick()['state'],'no_fresh_load');self.assertFalse(f.calls.exists())
    def test_mode_changed_during_native_read_prevents_action(self):
        f=self.fixture();f.tick();f.fresh()
        f.on_read=lambda:f.mode.write_text('off\n')
        self.assertEqual(f.tick()['state'],'context_changed');self.assertFalse(f.calls.exists())
    def test_receiver_epoch_change_reprimes_and_old_page_connection_is_closed(self):
        f=self.fixture();f.tick();f.fresh();f.process=('boot',42,'101')
        self.assertEqual(f.tick()['state'],'no_fresh_load')
        f.target='new-target';self.assertEqual(f.tick()['state'],'observer_unavailable')
        self.assertTrue(f.readers[0].closed)
        self.assertFalse(f.calls.exists())
        self.assertEqual(f.tick()['state'],'no_fresh_load')
    def test_first_seen_new_request_requires_actual_network_proof_in_adapter(self):
        f=self.fixture();f.tick();f.now+=1;f.video='BBBBBBBBBBB'
        url=URL+'&zx=new'
        f.snapshots=[snapshot(frame([load(2,f.video)]),url)]
        self.assertEqual(f.tick()['state'],'no_fresh_load');self.assertFalse(f.calls.exists())
        f=self.fixture();f.tick();f.now+=1;f.video='BBBBBBBBBBB'
        url=URL+'&zx=new';f.snapshots=[snapshot(frame([load(2,f.video)]),url)]
        def proof():
            handler=f.runtime.reader.event_handler
            handler({'method':'Network.requestWillBeSent','params':{'requestId':'new','request':{'url':url}}})
            handler({'method':'Network.responseReceived','params':{'requestId':'new','response':{'url':url,'status':200}}})
        f.on_read=proof
        self.assertEqual(f.tick()['state'],'youtube_verified')
    def test_permission_error_reports_only_sanitized_code_site(self):
        f=self.fixture()
        def denied():raise PermissionError('private-token-do-not-log')
        f.runtime.reader_factory=denied
        result=f.tick()
        self.assertEqual(result['state'],'observer_unavailable')
        self.assertEqual(result['error_type'],'PermissionError')
        self.assertIn(':denied:',result['error_site'])
        self.assertNotIn('private-token',json.dumps(result))

    def test_observation_only_never_actuates_or_publishes(self):
        f=self.fixture();f.runtime.actuate=False;f.tick();f.fresh()
        result=f.tick();self.assertEqual(result['state'],'observation_only')
        self.assertEqual(result['fresh_load_count'],1)
        self.assertFalse(f.calls.exists());self.assertEqual(f.published,[])
    def test_foreground_and_overlay_readback_are_required(self):
        f=self.fixture();f.tick();f.fresh()
        f.chrome['_youtube_windows']=lambda _:[]
        self.assertEqual(f.tick()['state'],'handoff_unverified')
        self.assertTrue(f.calls.exists());self.assertEqual(f.published,[])
    def test_handoff_refuses_changed_expected_context_before_spawning(self):
        f=self.fixture();before=f.runtime.context();f.mode.write_text('off\n')
        with self.assertRaises(RuntimeError):
            f.runtime.handoff('a'*64,expected_context=before,timeout=40)
        self.assertFalse(f.calls.exists())
    def test_active_pending_mode_and_busy_tick_never_create_second_action(self):
        f=self.fixture();f.player.MODE_REQUEST_FILE.write_text('off')
        self.assertEqual(f.tick()['state'],'context_changed')
        self.assertFalse(f.calls.exists())
        f.runtime._lock.acquire()
        try:self.assertEqual(f.tick()['state'],'busy')
        finally:f.runtime._lock.release()

if __name__=='__main__':unittest.main()
