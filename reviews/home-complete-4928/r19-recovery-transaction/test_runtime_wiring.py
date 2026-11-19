import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from typing import Any
from playback_intent import PlaybackIntent
from patch_runtime_wiring import patch_app,patch_player,APP_HELPER

ROOT=Path(__file__).resolve().parent

class WiringTests(unittest.TestCase):
    def player_functions(self):
        source=(ROOT/'work/backend/player.py').read_text()
        names={'configure_hls_recovery_tick','_progress_monitor'}
        nodes=[n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name in names]
        self.assertEqual({n.name for n in nodes},names)
        events=[]
        def sleep(_):
            events.append('sleep');raise StopIteration()
        ns=dict(_HLS_RECOVERY_TICK=None,save_current_progress=lambda reason:events.append('save'),
                WATCH_INTERVAL_SECONDS=1,time=SimpleNamespace(sleep=sleep))
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<actual-monitor>','exec'),ns)
        return ns,events
    def test_existing_monitor_serially_saves_recovers_and_sleeps(self):
        ns,events=self.player_functions()
        ns['configure_hls_recovery_tick'](lambda:events.append('recover'))
        with self.assertRaises(StopIteration):ns['_progress_monitor']()
        self.assertEqual(events,['save','recover','sleep'])
    def test_recovery_exception_does_not_stop_progress_monitor(self):
        ns,events=self.player_functions()
        def fail():events.append('recover');raise OSError()
        ns['configure_hls_recovery_tick'](fail)
        with self.assertRaises(StopIteration):ns['_progress_monitor']()
        self.assertEqual(events,['save','recover','sleep'])
    def test_progress_exception_does_not_disable_recovery(self):
        ns,events=self.player_functions()
        ns['save_current_progress']=lambda _:(_ for _ in ()).throw(OSError())
        ns['configure_hls_recovery_tick'](lambda:events.append('recover'))
        with self.assertRaises(StopIteration):ns['_progress_monitor']()
        self.assertEqual(events,['recover','sleep'])
    def test_callback_is_disabled_until_explicit_wiring_and_invalid_callbacks_refused(self):
        ns,events=self.player_functions()
        with self.assertRaises(ValueError):ns['configure_hls_recovery_tick'](None)
        with self.assertRaises(StopIteration):ns['_progress_monitor']()
        self.assertEqual(events,['save','sleep'])
    def test_actual_mode_entry_invalidates_intent_before_any_mode_probe(self):
        gate=PlaybackIntent();source=(ROOT/'work/backend/player.py').read_text()
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='switch_mode')
        def overview():self.assertIsNone(gate.read())
        ns=dict(Any=Any,_PLAYBACK_INTENT=gate,_exit_gnome_overview=overview,
                mode_status=lambda **k:{'mode':'games'},_set_display_refresh=lambda _:None)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<actual-mode-entry>','exec'),ns)
        self.assertEqual(ns['switch_mode']('games')['transition'],'none')
        self.assertEqual(gate.read(),1)
    def test_reviewed_source_drift_is_refused(self):
        app=(ROOT/'app.py.before-runtime-wiring').read_text()
        player=(ROOT/'player.py.before-runtime-wiring').read_text()
        self.assertIn('configure_hls_recovery_tick',patch_app(app))
        self.assertIn('@_PLAYBACK_INTENT.operation\ndef switch_mode',patch_player(player))
        for fn,source in [(patch_app,app),(patch_player,player)]:
            with self.assertRaises(ValueError):fn(source+'\n')
    def app_namespace(self,mode='mpv'):
        writes=[];runtime=SimpleNamespace(root=Path('/fixture/private'),
            run_once=lambda:dict(state='recovered',loaded=True,attempt=1,signed_url='never copy'))
        ns=dict(player=SimpleNamespace(_current_tv_mode=lambda:mode,
                   _atomic_private=lambda path,value:writes.append(value)),
                time=SimpleNamespace(time=lambda:123))
        exec(APP_HELPER,ns);ns['_HLS_RECOVERY_RUNTIME']=runtime
        return ns,writes
    def test_tick_persists_only_allowlisted_result_fields(self):
        ns,writes=self.app_namespace();ns['_hls_recovery_tick']()
        self.assertEqual(writes,[dict(schema='skeleton.hls-recovery-result.v1',
            checked_at=123,state='recovered',loaded=True,attempt=1)])
    def test_browser_mode_does_not_call_or_create_recovery(self):
        ns,writes=self.app_namespace('kiosk')
        ns['_HLS_RECOVERY_RUNTIME'].run_once=lambda:self.fail('browser touched')
        ns['_hls_recovery_tick']();self.assertEqual(writes,[])

if __name__=='__main__':unittest.main()
