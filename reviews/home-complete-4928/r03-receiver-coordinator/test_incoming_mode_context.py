import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from incoming_youtube_context import mode_token
from patch_incoming_youtube_handoff import INCOMING_FUNCTIONS,patch_wrapper,patch_controller

class ModeGuardTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name);current=root/'current';current.write_text('games\n')
        return root,current
    def guarded(self,root,current,token,body=':'):
        log=root/'actions'
        script='set -e\nSTATE_DIR='+repr(str(root))+'\nCURRENT_FILE='+repr(str(current))+'\n'
        script+=INCOMING_FUNCTIONS+'\n'
        script+='start_kiosk_incoming(){ echo action >> '+repr(str(log))+'; '+body+'; }\n'
        script+='start_kiosk_incoming_guarded '+repr('a'*64)+' '+repr(token)+'\n'
        result=subprocess.run(['/bin/bash','-c',script],text=True,capture_output=True,timeout=5)
        return result.returncode,log.exists()
    def test_valid_mode_generation_enters_guarded_handoff(self):
        root,current=self.fixture();self.assertEqual(self.guarded(root,current,mode_token(current)),(0,True))
    def test_missing_bad_or_changed_generation_never_enters_handoff(self):
        for token in ('','invalid','b'*64):
            root,current=self.fixture();self.assertNotEqual(self.guarded(root,current,token)[0],0)
            self.assertFalse((root/'actions').exists())
    def test_pending_user_mode_prevents_all_actions(self):
        root,current=self.fixture();(root/'requested').write_text('off')
        self.assertEqual(self.guarded(root,current,mode_token(current)),(1,False))
    def test_newer_user_request_during_handoff_prevents_commit(self):
        root,current=self.fixture()
        body='echo off > '+repr(str(root/'requested'))
        self.assertEqual(self.guarded(root,current,mode_token(current),body),(1,True))
    def test_same_mode_rewrite_changes_the_generation_token(self):
        root,current=self.fixture();before=mode_token(current);st=current.stat()
        os.utime(current,ns=(st.st_atime_ns,st.st_mtime_ns+1))
        self.assertNotEqual(mode_token(current),before)
        self.assertEqual(self.guarded(root,current,before),(1,False))
    def test_incoming_wrapper_never_removes_another_pending_request(self):
        root,current=self.fixture()
        path=root/'.local/bin';path.mkdir(parents=True)
        state=root/'.local/state/tv-mode';state.mkdir(parents=True)
        requested=state/'requested';requested.write_text('off\n')
        real=path/'tv-mode.real';real.write_text('#!/bin/bash\nexit 0\n');real.chmod(0o700)
        source=(Path(__file__).parent/'tv-mode.before-r03').read_text()
        wrapper=path/'tv-mode';wrapper.write_text(patch_wrapper(source));wrapper.chmod(0o700)
        result=subprocess.run(['/bin/bash',str(wrapper),'kiosk-incoming','a'*64,'b'*64],
            env={**os.environ,'HOME':str(root)},text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0)
        self.assertEqual(requested.read_text(),'off\n')
    def test_ordinary_wrapper_request_behavior_is_retained(self):
        root,current=self.fixture();path=root/'.local/bin';path.mkdir(parents=True)
        real=path/'tv-mode.real';real.write_text('#!/bin/bash\nexit 0\n');real.chmod(0o700)
        source=(Path(__file__).parent/'tv-mode.before-r03').read_text()
        wrapper=path/'tv-mode';wrapper.write_text(patch_wrapper(source))
        result=subprocess.run(['/bin/bash',str(wrapper),'kiosk'],
            env={**os.environ,'HOME':str(root)},text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0)
        self.assertFalse((root/'.local/state/tv-mode/requested').exists())
    def test_incoming_park_calls_the_existing_helper_only_under_matching_context(self):
        root,current=self.fixture();current.write_text('mpv\n')
        library=root/'.local/lib/skeleton-cast';library.mkdir(parents=True)
        (library/'incoming_youtube_context.py').write_text(
            (Path(__file__).parent/'incoming_youtube_context.py').read_text())
        called=root/'parked.json'
        (library/'mpv_browser_park.py').write_text(
            'import json,pathlib\nclass MpvIPC:\n def __init__(self,path):self.path=path\n'
            'def park_paused_mpv(command):\n pathlib.Path('+repr(str(called))+').write_text(json.dumps(command.path))\n')
        script='set -e\nSTATE_DIR='+repr(str(root))+'\nCURRENT_FILE='+repr(str(current))+'\n'
        script+='INCOMING_EXPECTED_CONTEXT='+repr(mode_token(current))+'\n'+INCOMING_FUNCTIONS
        script+='\nprocess_running(){ return 0; }\nstop_mpv(){ return 99; }\nhide_parked_mpv_for_incoming\n'
        result=subprocess.run(['/bin/bash','-c',script],text=True,capture_output=True,timeout=5,
            env={**os.environ,'HOME':str(root)})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(called.read_text()),
                         '/run/user/'+str(os.getuid())+'/skeleton-cast-mpv.sock')
        called.unlink();(root/'requested').write_text('off')
        result=subprocess.run(['/bin/bash','-c',script],text=True,capture_output=True,timeout=5,
            env={**os.environ,'HOME':str(root)})
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(called.exists())



class IncomingFocusTests(unittest.TestCase):
    def test_focus_targets_existing_page_without_loading_or_resuming_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);record=root/'calls.json';helper=root/'chrome'
            helper.write_text('#!/usr/bin/python3\nimport json,pathlib\ndef ws_method(method,params):\n pathlib.Path('+repr(str(record))+').write_text(json.dumps([method,params]))\n')
            helper.chmod(0o700)
            script='set -e\nCHROME_MEDIA='+repr(str(helper))+'\n'+INCOMING_FUNCTIONS+'\nfocus_incoming_chrome\n'
            result=subprocess.run(['/bin/bash','-c',script],text=True,capture_output=True,timeout=5,
                                  env={**os.environ,'HOME':str(root)})
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(record.read_text()),['Page.bringToFront',{}])



class IncomingPauseFailureTests(unittest.TestCase):
    def invoke(self,*,live=False,pause_ok=False,valid=True):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bin=root/'.local/bin';bin.mkdir(parents=True)
            state=root/'.local/state/skeleton-cast';state.mkdir(parents=True)
            record={'job_id':'a'*16,'pid':42,'live':live} if valid else {}
            (state/'current.json').write_text(json.dumps(record))
            called=root/'paused';stopped=root/'stopped'
            helper=bin/'skeleton-cast-control'
            helper.write_text('#!/bin/bash\necho pause >> '+repr(str(called))+'\nexit '+str(0 if pause_ok else 1)+'\n')
            helper.chmod(0o700)
            script='set -e\n'+INCOMING_FUNCTIONS+'\nprocess_running(){ return 0; }\n'
            script+='stop_mpv(){ echo stop >> '+repr(str(stopped))+'; }\npark_mpv_for_incoming\n'
            result=subprocess.run(['/bin/bash','-c',script],text=True,capture_output=True,timeout=5,
                                  env={**os.environ,'HOME':str(root)})
            return result.returncode,called.exists(),stopped.exists()
    def test_failed_vod_pause_does_not_fall_back_to_killing_preserved_work(self):
        self.assertEqual(self.invoke(),(1,True,False))
    def test_successful_vod_pause_keeps_the_player(self):
        self.assertEqual(self.invoke(pause_ok=True),(0,True,False))
    def test_live_tv_is_stopped_instead_of_accumulating_parked_buffer(self):
        self.assertEqual(self.invoke(live=True),(0,False,True))
    def test_unknown_current_record_never_inherits_stop_or_pause_action(self):
        self.assertEqual(self.invoke(valid=False),(1,False,False))

if __name__=='__main__':unittest.main()
