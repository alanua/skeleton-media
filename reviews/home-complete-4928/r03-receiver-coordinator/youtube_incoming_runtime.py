"""Existing-backend receiver observation and canonical incoming handoff adapter.

No new thread/daemon/executor, page hook, pairing mutation or ordinary media play.
Call tick serially from an existing monitor. actuation is explicit and defaults off.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import signal
import subprocess
import threading
import time
import traceback
from urllib.parse import urlsplit

from incoming_youtube_context import mode_token
from youtube_incoming_coordinator import IncomingYouTubeCoordinator, ReceiverEventLedger
from youtube_lounge_xhr_observer import connect_reader, SnapshotTracker
from youtube_receiver_request_proof import ReceiverRequestProofs


def owned_chrome_process(pid,proc_root=Path('/proc')):
    """Read canonical unit PID identity without ptrace-gated exe resolution."""
    if type(pid) is not int or pid<=0:raise RuntimeError('Chrome PID unavailable')
    root=Path(proc_root);proc=root/str(pid)
    if proc.stat().st_uid!=os.getuid() or (proc/'comm').read_text().strip()!='chrome':
        raise RuntimeError('Chrome process ownership unavailable')
    tail=(proc/'stat').read_text().rsplit(')',1)[1].split()
    if len(tail)<20 or not tail[19].isdigit():raise RuntimeError('Chrome start identity unavailable')
    boot=(root/'sys/kernel/random/boot_id').read_text().strip()
    if not boot:raise RuntimeError('boot identity unavailable')
    return (boot,pid,tail[19])


class IncomingYouTubeRuntime:
    def __init__(self,*,app,player,root,actuate=False,clock=time.monotonic,
                 reader_factory=connect_reader,chrome=None):
        self.app,self.player=app,player;self.root=Path(root)
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if (self.root.is_symlink() or self.root.stat().st_uid!=os.getuid()
                or self.root.stat().st_mode & 0o077):raise ValueError('private receiver state required')
        self.actuate=actuate is True;self.clock=clock;self.reader_factory=reader_factory
        self.chrome=chrome if chrome is not None else runpy.run_path(str(Path.home()/'.local/bin/home-edge-chrome-media'))
        self.reader=None;self.tracker=None;self.proofs=None;self.window_context=None
        self.last_observed=None;self.process_identity=None;self._lock=threading.Lock()
        self.coordinator=IncomingYouTubeCoordinator(
            ledger=ReceiverEventLedger(self.root/'ledger/events.sqlite3'),
            context=self.context,status=self.status,handoff=self.handoff,
            verify=self.verify,publish=self.publish,clock=clock)

    def _page(self):
        pages=self.chrome['http_json']('/json/list')
        def receiver(page):
            try:
                u=urlsplit(page.get('url',''))
                return (page.get('type')=='page' and u.scheme=='https'
                    and u.hostname in ('youtube.com','www.youtube.com') and u.path=='/tv')
            except (ValueError,TypeError):return False
        matches=[p for p in pages if isinstance(p,dict) and receiver(p)]
        if len(matches)!=1:raise RuntimeError('ambiguous receiver target')
        target=matches[0].get('id')
        if not isinstance(target,str) or not 1<=len(target)<=256:raise RuntimeError('receiver target unavailable')
        return target

    def _process(self):
        if self.process_identity is None:
            path=Path.home()/'.local/state/tv-mode/backend-chrome.unit'
            unit=path.read_text().strip()
            if not re.fullmatch(r'home-edge-media-backend-chrome-\d+\.service',unit):
                raise RuntimeError('canonical Chrome unit unavailable')
            reply=subprocess.run(['systemctl','--user','show',unit,'-p','MainPID','--value'],
                capture_output=True,text=True,timeout=3,
                env={**os.environ,'XDG_RUNTIME_DIR':'/run/user/'+str(os.getuid()),
                     'DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/'+str(os.getuid())+'/bus'})
            if reply.returncode or not reply.stdout.strip().isdigit():
                raise RuntimeError('Chrome PID unavailable')
            self.process_identity=owned_chrome_process(int(reply.stdout.strip()))
        observed=owned_chrome_process(self.process_identity[1])
        if observed!=self.process_identity:raise RuntimeError('Chrome process changed')
        return observed

    def context(self):
        p=self.player
        target=self._page()
        if self.reader is not None and target!=self.reader.target_id:
            raise RuntimeError('receiver target changed')
        process=self._process()
        epoch=hashlib.sha256(json.dumps([process,target],separators=(',',':')).encode()).hexdigest()
        return dict(endpoint='home_edge_01',receiver_epoch=epoch,
            mode=p._current_tv_mode(),mode_token=mode_token(p.MODE_FILE),
            revision=p.playback_control_revision(),
            transition=p.MODE_REQUEST_FILE.exists() or p._mode_transition_target() is not None)

    def status(self):
        return self.chrome['status']()

    def verify(self):
        context=self.context()
        visible=self.chrome['_youtube_windows'](True)
        state=self.chrome['window_state']()
        result=subprocess.run(['/usr/bin/xdotool','getactivewindow'],
            env=self.chrome['_x11_env'](),capture_output=True,text=True,timeout=2)
        active=result.stdout.strip() if result.returncode==0 else ''
        foreground=active in visible and state.get('window_state')=='fullscreen'
        if foreground:
            owner=subprocess.run(['/usr/bin/xdotool','getwindowpid',active],
                env=self.chrome['_x11_env'](),capture_output=True,text=True,timeout=2)
            foreground=owner.returncode==0 and owner.stdout.strip()==str(self._process()[1])
        p=self.player
        if p.SOCKET.exists():
            def get(name):
                reply=p.command(['get_property',name],timeout=.25)
                if reply.get('error')!='success':raise RuntimeError('parked player unavailable')
                return reply.get('data')
            hidden=(get('pause') is True and get('fullscreen') is False
                    and get('ontop') is False and get('window-minimized') is True)
        else:
            hidden=not p._named_process_running('mpv')
        return dict(endpoint='home_edge_01',receiver_epoch=context['receiver_epoch'],
                    mode=context['mode'],foreground_chrome=bool(foreground),
                    mpv_overlay_hidden=hidden,player=self.status())

    def handoff(self,video_digest,*,expected_context,timeout):
        if not self.actuate:raise RuntimeError('receiver actuation disabled')
        if self.context()!=expected_context:raise RuntimeError('incoming context changed')
        if not re.fullmatch('[a-f0-9]{64}',video_digest) or timeout!=40:
            raise ValueError('unsupported incoming operation')
        def apply():
            # Invalidate prepared HLS recovery before requesting the canonical
            # controller. No wire lock is held while it parks MPV through the API.
            process=subprocess.Popen([self.player.TV_MODE,'kiosk-incoming',
                video_digest,expected_context['mode_token']],stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            try:
                code=process.wait(timeout=timeout)
            except BaseException:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=2)
                raise RuntimeError('incoming controller deadline') from None
            if code:raise RuntimeError('incoming controller rejected context or postcondition')
        self.player._PLAYBACK_INTENT.operation(apply)()

    def publish(self,event):
        if not self.actuate:raise RuntimeError('receiver publication disabled')
        mode=self.player.mode_status()
        if mode.get('mode')!='kiosk':raise RuntimeError('YouTube mode changed before publication')
        self.app.media_state.publish(mode={**mode,'endpoint_id':'home_edge_01'},
            player=self.status(),reason='fresh_receiver_media_load')

    def _connect(self):
        self.reader=self.reader_factory()
        context=self.context()
        self.proofs=ReceiverRequestProofs(context['receiver_epoch'],clock=self.clock)
        self.reader.event_handler=self.proofs.event
        self.reader.enable_network_observation()
        self.tracker=SnapshotTracker()
        self.window_context=None;self.last_observed=None

    def close(self):
        reader=self.reader;self.reader=None;self.tracker=None;self.proofs=None
        self.window_context=None;self.last_observed=None;self.process_identity=None
        if reader is not None:
            try:reader.close()
            except Exception:pass

    def tick(self):
        if not self._lock.acquire(blocking=False):return {'state':'busy'}
        phase='connect'
        try:
            if self.reader is None:self._connect()
            phase='context'
            before=self.context();now=self.clock()
            changed=(before!=self.window_context or self.last_observed is None
                     or not 0<=now-self.last_observed<=12)
            if changed:self.tracker=SnapshotTracker()
            started=now if changed else self.last_observed
            phase='snapshot'
            snapshots=self.reader.snapshot()
            after=self.context()
            if before!=after or before.get('revision') is None or before.get('transition'):
                self.tracker=SnapshotTracker();self.tracker.consume(snapshots)
                self.window_context=None;self.last_observed=self.clock()
                return {'state':'context_changed'}
            events=self.tracker.consume(snapshots)
            self.window_context=deepcopy(before);self.last_observed=self.clock()
            if not self.actuate:
                return {'state':'observation_only','fresh_load_count':len(events),
                        'receiver_wire_contract_observed':self.tracker.report()['receiver_wire_contract_observed']}
            phase='coordinate'
            return self.coordinator.handle(events,window_context=before,
                window_started=started,request_proofs=self.proofs.snapshot())
        except Exception as error:
            self.close()
            frames=traceback.extract_tb(error.__traceback__)
            site=frames[-1] if frames else None
            return {'state':'observer_unavailable','phase':phase,'error_type':type(error).__name__,
                    'error_site':(Path(site.filename).name+':'+site.name+':'+str(site.lineno))[:160] if site else 'unknown'}
        finally:self._lock.release()
