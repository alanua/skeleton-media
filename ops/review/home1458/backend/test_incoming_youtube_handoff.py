"""Isolated shell harness: no real player, controller, input or device actions."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from patch_incoming_youtube_handoff import INCOMING_FUNCTIONS, patch_controller, patch_wrapper


VIDEO = 'AAAAAAAAAAA'
DIGEST = hashlib.sha256(VIDEO.encode()).hexdigest()


class IncomingHandoffTests(unittest.TestCase):
    def run_handoff(self, *, initial=None, final=None, digest=DIGEST, chrome=True, focus=True):
        initial = initial if initial is not None else {'running':True,'pause':False,'player_state':1,'video_id':VIDEO}
        final = final if final is not None else initial
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); log=p/'events'; count=p/'count'; data=p/'status.json'
            data.write_text(json.dumps([initial,final]))
            helper=p/'chrome'; helper.write_text('#!/usr/bin/python3\nimport json,pathlib\np=pathlib.Path('+repr(str(count))+')\nn=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\nprint(json.dumps(json.loads(pathlib.Path('+repr(str(data))+').read_text())[min(n,1)]))\n'); helper.chmod(0o700)
            script='set -e\nCHROME_MEDIA='+str(helper)+'\n'
            script+='event(){ echo "$1" >> '+str(log)+'; }\n'
            for name in ['exit_gnome_overview','park_or_stop_mpv_for_browser','stop_games','hide_parked_mpv_for_browser','switch_tty2']:
                script+=name+'(){ event '+name+'; }\n'
            for name in ['restore_youtube','safe_start_volume','normalize_volume','ensure_hyperhdr','stop_chrome','start_chrome_cold']:
                script+=name+'(){ event forbidden_'+name+'; return 90; }\n'
            script+='process_running(){ return '+str(0 if chrome else 1)+'; }\n'
            script+='focus_chrome_wait(){ event focus; return '+str(0 if focus else 1)+'; }\n'
            script+='sleep(){ :; }\nlog(){ :; }\n'+INCOMING_FUNCTIONS+'\nstart_kiosk_incoming '+repr(digest)+'\nevent committed\n'
            result=subprocess.run(['/bin/bash','-c',script],capture_output=True,text=True,timeout=5)
            return result.returncode, log.read_text().splitlines() if log.exists() else []

    def test_preserves_incoming_media_and_parks_before_focus(self):
        rc, events=self.run_handoff(); self.assertEqual(rc,0)
        self.assertEqual(events[-1],'committed'); self.assertNotIn('restore_youtube',events)
        self.assertLess(events.index('hide_parked_mpv_for_browser'),events.index('focus'))
        self.assertFalse(any(x.startswith('forbidden_') for x in events))
    def test_mismatched_clip_refuses_before_any_surface_change(self):
        rc,events=self.run_handoff(digest='b'*64); self.assertNotEqual(rc,0); self.assertEqual(events,[])
    def test_missing_event_digest_refuses_before_any_change(self):
        rc,events=self.run_handoff(digest=''); self.assertNotEqual(rc,0); self.assertEqual(events,[])
    def test_paused_or_stopped_browser_is_not_new_playback(self):
        for state in [{'running':True,'pause':True,'player_state':2,'video_id':VIDEO},{'running':False,'pause':False,'player_state':1,'video_id':VIDEO}]:
            rc,events=self.run_handoff(initial=state); self.assertNotEqual(rc,0); self.assertEqual(events,[])
    def test_no_browser_never_cold_starts(self):
        rc,events=self.run_handoff(chrome=False); self.assertNotEqual(rc,0); self.assertEqual(events,[])
    def test_invalid_status_does_not_count_boolean_as_player_state(self):
        for status in [[],{'running':True,'pause':False,'player_state':True,'video_id':VIDEO}]:
            rc,events=self.run_handoff(initial=status); self.assertNotEqual(rc,0); self.assertEqual(events,[])
    def test_changed_clip_does_not_commit_stale_handoff(self):
        rc,events=self.run_handoff(final={'running':True,'pause':False,'player_state':1,'video_id':'BBBBBBBBBBB'}); self.assertNotEqual(rc,0); self.assertNotIn('committed',events)
    def test_unverified_focus_does_not_commit(self):
        rc,events=self.run_handoff(focus=False); self.assertNotEqual(rc,0); self.assertNotIn('committed',events)
    def test_buffering_incoming_clip_can_be_shown_without_playback_input(self):
        rc,events=self.run_handoff(initial={'running':True,'pause':False,'player_state':3,'video_id':VIDEO}); self.assertEqual(rc,0); self.assertIn('committed',events)
    def test_patch_exact_dispatch_footer_and_idempotency(self):
        base='start_kiosk() {\n    :\n}\ncase "$MODE" in\n    kiosk) log "switch kiosk"; start_kiosk ;;\nesac\nif [[ "$MODE" != off ]]; then normalize_volume; ensure_hyperhdr; fi\n'
        patched=patch_controller(base)
        self.assertIn('start_kiosk_incoming "${2:-}" || exit $?',patched)
        self.assertIn('${INCOMING_YOUTUBE_HANDOFF:-0}',patched)
        subprocess.run(['/bin/bash','-n'],input=patched,text=True,check=True)
        with self.assertRaises(ValueError):patch_controller(patched)
        with self.assertRaises(ValueError):patch_controller(base.replace('normalize_volume','other'))
    def test_wrapper_normalizes_incoming_requested_mode(self):
        base='case "${1:-}" in\n  chrome|kiosk|mpv|games|off) requested="${1:-}" ;;\n  *) requested="" ;;\nesac\n'
        patched=patch_wrapper(base); self.assertIn('kiosk-incoming) requested=kiosk ;;',patched)
        with self.assertRaises(ValueError):patch_wrapper(patched)


if __name__=='__main__':unittest.main()
