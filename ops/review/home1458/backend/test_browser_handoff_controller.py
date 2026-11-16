"""Run isolated controller functions with fake backends, never the live controller."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class HandoffTests(unittest.TestCase):
    def function(self,name):
        path=Path(os.environ['PRIVATE_SOURCE_ROOT'])/'tv-mode.real'
        source=path.read_text();start=source.index(name+'() {');end=source.index('\n}',start)+2
        return source[start:end]
    def run_handoff(self,entry='start_kiosk',warm=True,park_error=False,mpv=True):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);log=root/'events';helper=root/'.local/lib/skeleton-cast/mpv_browser_park.py';helper.parent.mkdir(parents=True)
            helper.write_text('import os,pathlib,sys\np=pathlib.Path(os.environ["EVENTS"])\nwith p.open("a") as f:f.write("park_surface\\n")\nsys.exit('+str(1 if park_error else 0)+')\n')
            chrome=root/'chrome';chrome.write_text('#!/bin/sh\necho chrome_ready >> "$EVENTS"\nexit 0\n');chrome.chmod(0o700)
            code='''set -e
event(){ echo "$1" >> "$EVENTS"; }
exit_gnome_overview(){ :; }
safe_start_volume(){ :; }
park_or_stop_mpv_for_browser(){ event pause_mpv; }
stop_games(){ :; }
restore_youtube(){ event restore_browser; }
focus_chrome(){ event focus_browser; }
focus_chrome_wait(){ event focus_browser; }
stop_chrome(){ event stop_chrome; }
start_chrome_cold(){ event cold_browser_ready; }
stop_mpv(){ event stop_mpv; }
switch_tty2(){ event switch_tty; }
log(){ :; }
sleep(){ :; }
'''
            code+='process_running(){ if [ "$1" = mpv ]; then return '+str(0 if mpv else 1)+'; else return '+str(0 if warm else 1)+'; fi; }\n'
            code+='\n'.join(self.function(n) for n in ['hide_parked_mpv_for_browser',entry])+'\n'+entry
            subprocess.run(['/bin/bash','-c',code],env={**os.environ,'HOME':tmp,'EVENTS':str(log),'CHROME_MEDIA':str(chrome)},check=True,timeout=5,capture_output=True)
            return log.read_text().splitlines()
    def test_warm_park_follows_browser_ready_and_precedes_focus(self):
        events=self.run_handoff();self.assertLess(events.index('restore_browser'),events.index('park_surface'));self.assertLess(events.index('park_surface'),events.index('switch_tty'))
        self.assertNotIn('stop_mpv',events)
    def test_cold_park_follows_chrome_ready(self):
        events=self.run_handoff(warm=False);self.assertLess(events.index('cold_browser_ready'),events.index('park_surface'))
    def test_unverified_park_uses_existing_player_only_fallback(self):
        events=self.run_handoff(park_error=True);self.assertEqual(events.count('stop_mpv'),1);self.assertNotIn('stop_chrome',events)
    def test_no_mpv_means_no_park(self):
        events=self.run_handoff(mpv=False);self.assertNotIn('park_surface',events)
    def test_browser_mode_uses_same_surface_guard(self):
        events=self.run_handoff(entry='start_chrome');self.assertIn('park_surface',events)


if __name__=='__main__':unittest.main()
