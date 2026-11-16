"""Actual watchdog collect() under synthetic services, IPC and prior snapshots."""
import ast
import os
from pathlib import Path
import types
import unittest
from youtube_playback_health import youtube_progress_health


ROOT=Path(os.environ['PRIVATE_SOURCE_ROOT'])


class HealthIntegrationTests(unittest.TestCase):
    def sample(self,**values):
        return {**{'running':True,'backend':'youtube-web','url':'https://www.youtube.com/tv',
            'video_id':'AAAAAAAAAAA','pause':False,'time-pos':10,'duration':100,'ready_state':4,
            'video_width':1280,'decoded_frames':100,'playback_rate':1},**values}
    def collect(self,current,previous=None):
        source=(ROOT/'media_health.py').read_text();tree=ast.parse(source);scope={'Any':object}
        node=next(n for n in tree.body if isinstance(n,ast.AnnAssign) and n.target.id=='DEFAULT_CONFIG')
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<synthetic config>','exec'),scope)
        cfg=scope['DEFAULT_CONFIG']
        def read_json(path,default=None):
            if path=='last':return previous or {}
            return {'schema':'skeleton.media.state.v1','player':current}
        scope.update({'load_config':lambda:cfg,'time':types.SimpleNamespace(time=lambda:1000,
                strftime=lambda *args:'synthetic',gmtime=lambda *args:None),
            'mode_status':lambda:{'mode':'kiosk','raw_mode':'kiosk','active_tty':'tty2','chrome':True},
            'MODE_REQUESTED':types.SimpleNamespace(exists=lambda:False),
            'service_state':lambda unit:'active','http_json':lambda *args:({'status':'ok'},None,200),
            'MEDIA_STATE':'shared','LAST_FILE':'last','read_json':read_json,
            'process_exists':lambda *args,**kwargs:False,
            'json_command':lambda *args:(current,{'rc':0,'stderr':''}),
            'HOME':Path('/synthetic'),'urlsplit':__import__('urllib.parse',fromlist=['urlsplit']).urlsplit,
            'CAST_STATE':types.SimpleNamespace(glob=lambda _:[]),
            'run':lambda argv,*args:{'rc':0,'stdout':'Volume: 0.5' if argv[0]=='wpctl' else '', 'stderr':''},
            'HYPERHDR_DESIRED':types.SimpleNamespace(read_text=lambda **kwargs:'off'),
            'shutil':types.SimpleNamespace(disk_usage=lambda _:types.SimpleNamespace(free=100*1024**3,total=200*1024**3)),
            'available_memory':lambda:8*1024**3,'youtube_progress_health':youtube_progress_health})
        for name in ['sanitize_player','collect']:
            node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
            exec(compile(ast.Module(body=[node],type_ignores=[]),'<isolated watchdog>','exec'),scope)
        return scope['collect']()
    def previous(self,**changes):
        raw=self.sample(**changes);raw['time_pos']=raw.pop('time-pos')
        return {'mode':'kiosk','checked_at':996,'player':raw}
    def test_buffered_frozen_frame_cannot_be_healthy(self):
        result=self.collect(self.sample(),self.previous())
        self.assertEqual(result['playback_progress']['state'],'stalled');self.assertFalse(result['healthy'])
        self.assertEqual(result['overall'],'degraded')
    def test_moving_clock_with_frozen_video_is_degraded(self):
        result=self.collect(self.sample(**{'time-pos':14}),self.previous())
        self.assertEqual(result['playback_progress']['state'],'frame_stalled');self.assertFalse(result['healthy'])
    def test_real_clock_and_frame_progress_passes(self):
        result=self.collect(self.sample(**{'time-pos':14,'decoded_frames':200}),self.previous())
        self.assertTrue(result['healthy']);self.assertTrue(result['playback_progress']['healthy'])
    def test_paused_or_idle_page_does_not_trigger_backend_reload(self):
        for values,state in [({'pause':True},'paused'),({'running':False},'stopped')]:
            result=self.collect(self.sample(**values),self.previous())
            self.assertTrue(result['healthy']);self.assertEqual(result['playback_progress']['state'],state)
            self.assertFalse(result['playback_progress']['healthy'])
            self.assertEqual(next(c for c in result['checks'] if c['code']=='backend_kiosk')['status'],'ok')
    def test_missing_or_stale_samples_do_not_assert_playback_health(self):
        for previous in [None,{**self.previous(),'checked_at':100},{**self.previous(),'mode':'mpv'}]:
            result=self.collect(self.sample(),previous)
            self.assertFalse(result['playback_progress']['healthy']);self.assertFalse(result['healthy'])
    def test_sanitizer_retains_frame_probe_fields(self):
        result=self.collect(self.sample(seeking=True),self.previous())
        self.assertTrue(result['player']['seeking']);self.assertEqual(result['player']['decoded_frames'],100)
        self.assertEqual(result['playback_progress']['state'],'transition')
    def test_no_new_repair_or_actuation_path(self):
        original=ast.parse((ROOT.parent/'backend-baseline/media_health.py').read_text())
        patched=ast.parse((ROOT/'media_health.py').read_text())
        for name in ['select_repairs','execute_repair','watchdog']:
            a=next(n for n in original.body if isinstance(n,ast.FunctionDef) and n.name==name)
            b=next(n for n in patched.body if isinstance(n,ast.FunctionDef) and n.name==name)
            self.assertEqual(ast.dump(a,include_attributes=False),ast.dump(b,include_attributes=False))


if __name__=='__main__':unittest.main()
