import unittest
import contextlib
import io
from pathlib import Path
import tempfile
from unittest import mock
import mpv_browser_park
from mpv_browser_park import park_paused_mpv


class ParkTests(unittest.TestCase):
    def fake(self,**values):
        state={'pause':True,'fullscreen':True,'ontop':True,'window-minimized':False,**values};calls=[]
        def command(argv):
            calls.append(argv)
            if argv[0]=='set_property':state[argv[1]]=argv[2]
            return {'error':'success','data':state.get(argv[1])}
        return state,calls,command
    def test_hides_surface_without_starting_or_destroying_session(self):
        state,calls,command=self.fake();result=park_paused_mpv(command)
        self.assertTrue(result['surface_hidden']);self.assertTrue(state['pause'])
        self.assertEqual([x for x in calls if x[0]=='set_property'],[
            ['set_property','fullscreen',False],['set_property','ontop',False],['set_property','window-minimized',True]])
    def test_repeat_is_noop(self):
        state,calls,command=self.fake(**{'fullscreen':False,'ontop':False,'window-minimized':True})
        self.assertEqual(park_paused_mpv(command)['status'],'already_parked')
        self.assertFalse(any(x[0]=='set_property' for x in calls))
    def test_playing_session_is_not_touched(self):
        state,calls,command=self.fake(pause=False)
        with self.assertRaises(RuntimeError):park_paused_mpv(command)
        self.assertFalse(any(x[0]=='set_property' for x in calls))
    def test_successful_ipc_without_applied_minimize_is_not_success(self):
        state,calls,base=self.fake()
        def command(argv):
            if argv[:2]==['set_property','window-minimized']:return {'error':'success'}
            return base(argv)
        with self.assertRaises(RuntimeError):park_paused_mpv(command,lambda _:None)
    def test_unsupported_property_fails(self):
        def unavailable(argv):return {'error':'property unavailable'}
        with self.assertRaises(RuntimeError):park_paused_mpv(unavailable)
    def test_concurrent_resume_cannot_be_reported_hidden(self):
        state,calls,base=self.fake()
        def command(argv):
            result=base(argv)
            if argv[:2]==['set_property','window-minimized']:state['pause']=False
            return result
        with self.assertRaises(RuntimeError):park_paused_mpv(command)
    def test_mode_request_fences_cli_action(self):
        for mode,requested,allowed in [('mpv','kiosk',True),('kiosk',None,True),('kiosk','mpv',False),('mpv',None,False),('games',None,False)]:
            with self.subTest(mode=mode,requested=requested),tempfile.TemporaryDirectory() as tmp:
                home=Path(tmp);directory=home/'.local/state/tv-mode';directory.mkdir(parents=True);(directory/'current').write_text(mode)
                if requested is not None:(directory/'requested').write_text(requested)
                state,calls,command=self.fake()
                with mock.patch.object(mpv_browser_park.Path,'home',return_value=home),mock.patch.object(mpv_browser_park,'MpvIPC',return_value=command),contextlib.redirect_stdout(io.StringIO()):
                    if allowed:mpv_browser_park.main()
                    else:
                        with self.assertRaises(RuntimeError):mpv_browser_park.main()
                        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
