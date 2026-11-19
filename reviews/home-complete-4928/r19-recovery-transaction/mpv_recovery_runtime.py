"""Canonical in-process adapter for one bounded HLS recovery invocation.

No timer, daemon, application import, cold play, mode switch or automatic enable.
The existing backend supplies its reviewed app/player objects. All media changes
use its intent gate and the same flock as tv-mode.real.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import threading
import time

from bounded_hls_resolver import BoundedResolver, prepare_recovery_source
from hls_recovery_budget import SQLiteRecoveryBudget
from hls_recovery_transaction import HLSRecovery, RecoveryLoadPending
from mpv_decoded_image_evidence import MPVDecodedImageEvidence
from mpv_recovery_commit import RecoveryCommit
from mpv_recovery_postload import MPVPostloadSnapshot
from mpv_recovery_snapshot import MPVRecoverySnapshot, SnapshotUnavailable, canonical


def read_json(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise SnapshotUnavailable('private record unavailable')
    return value


@contextmanager
def mode_guard(path):
    """Use the exact existing tv-mode lock; never wait behind a mode change."""
    path = Path(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise SnapshotUnavailable('mode lock ownership unavailable')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def owned_mpv_process(current, proc_root=Path('/proc')):
    """Bind the current PID to boot ID, start ticks, owner and executable."""
    pid = current.get('pid')
    if type(pid) is not int or pid <= 0:
        raise SnapshotUnavailable('player PID unavailable')
    root = Path(proc_root)
    directory = root / str(pid)
    if directory.stat().st_uid != os.getuid():
        raise SnapshotUnavailable('different process owner')
    if (directory / 'exe').resolve(strict=True).name != 'mpv':
        raise SnapshotUnavailable('different executable')
    # The comm field may contain spaces or parentheses. Starttime is field 22.
    stat = (directory / 'stat').read_text()
    tail = stat[stat.rfind(')') + 2:].split()
    ticks = tail[19]
    if not ticks.isdigit():
        raise SnapshotUnavailable('process start unavailable')
    boot = (root / 'sys/kernel/random/boot_id').read_text().strip()
    if not boot:
        raise SnapshotUnavailable('boot identity unavailable')
    return (boot, pid, ticks)


class MPVRecoveryRuntime:
    def __init__(self, *, app, player, resolver_worker, root,
                 clock=time.monotonic, sleep=time.sleep):
        self.app, self.player = app, player
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (self.root.is_symlink() or self.root.stat().st_uid != os.getuid()
                or self.root.stat().st_mode & 0o077):
            raise ValueError('private recovery root required')
        self.clock, self.sleep = clock, sleep
        self.resolver = BoundedResolver(resolver_worker)
        self.engine = HLSRecovery()
        self.budget = SQLiteRecoveryBudget(self.root / 'budget' / 'attempts.sqlite3')
        self._run_lock = threading.Lock()
        self._deadline = None
        # Refuse activation with the old player; do not silently omit the fence.
        if not callable(getattr(player._PLAYBACK_INTENT, 'commit', None)):
            raise ValueError('reviewed playback commit gate required')

    def mode(self):
        p = self.player
        if p.MODE_REQUEST_FILE.exists() or p._mode_transition_target() is not None:
            return 'transition'
        return p._current_tv_mode()

    def process(self):
        return owned_mpv_process(read_json(self.player.CURRENT))

    def mutation_guard(self):
        return mode_guard(self.player.MODE_FILE.parent / 'lock')

    def _command(self, parts, *, timeout=.25, expected_revision=None):
        if self._deadline is not None and self.clock()+timeout > self._deadline:
            raise SnapshotUnavailable('recovery total deadline')
        if expected_revision is None:
            return self.player.command(parts, timeout=timeout)
        return self.player.command(parts, timeout=timeout, expected_revision=expected_revision)

    def _property(self, prop):
        reply = self._command(['get_property', prop], timeout=.25)
        if not isinstance(reply, dict) or reply.get('error') != 'success':
            raise SnapshotUnavailable('MPV observation unavailable')
        return reply.get('data')

    def _frame_context(self):
        return dict(mode=self.mode(), process=self.process(),
                    revision=self.player.playback_control_revision(),
                    path=self._property('path'), seeking=self._property('seeking'))

    def _image_command(self, parts, *, timeout):
        # screenshot-to-file is an observation. Public command() classifies
        # non-get commands as user intent and would invalidate this recovery.
        if (not isinstance(parts, list) or len(parts) != 3
                or parts[0] != 'screenshot-to-file' or parts[2] != 'video'
                or timeout != .8):
            raise SnapshotUnavailable('unsupported image observation')
        target = Path(parts[1])
        frame_root = (self.root / 'frames').resolve()
        if (target.name != 'decoded.png' or not target.is_absolute()
                or not target.resolve().is_relative_to(frame_root)
                or target.parent.parent.resolve() != frame_root):
            raise SnapshotUnavailable('foreign image destination')
        if self._deadline is not None and self.clock()+timeout > self._deadline:
            raise SnapshotUnavailable('recovery total deadline')
        return self.player._command_wire(parts, timeout=timeout)

    def _transport(self, job, source):
        # This exact helper also supports provider network refreshes; those
        # providers are excluded from this already-resolved HLS-only adapter.
        if (source.get('kind') != 'hls'
                or source.get('backend') in {'kodik', 'uacdn'}):
            raise SnapshotUnavailable('unsupported HLS transport')
        detached = deepcopy(source)
        url, _, options = self.player._mpv_source_configuration(
            deepcopy(job), detached, 'off')
        if url != source['url'] or canonical(detached) != canonical(source):
            raise SnapshotUnavailable('transport unexpectedly refreshed source')
        return {k: options[k] for k in
                ('referrer','user-agent','http-header-fields') if k in options}

    def run_once(self):
        if not self._run_lock.acquire(blocking=False):
            return {'state':'busy','loaded':False}
        try:
            self._deadline = self.clock()+45
            return self._run()
        except Exception:
            # URLs, resolver errors and private filesystem contents never escape.
            return {'state':'adapter_unavailable','loaded':False}
        finally:
            self._deadline = None
            self._run_lock.release()

    def _run(self):
        a, p = self.app, self.player
        if (self.mode() != 'mpv' or self._property('pause') is not False
                or self._property('paused-for-cache') is not True
                or self._property('seeking') is not False):
            return {'state':'ineligible','loaded':False}
        frames = MPVDecodedImageEvidence(context=self._frame_context,
            command=self._image_command, root=self.root / 'frames', clock=self.clock)
        reader = MPVRecoverySnapshot(
            record=lambda:read_json(p.CURRENT),
            job=lambda jid:read_json(a._job_path(jid)), mode=self.mode,
            process=self.process, revision=p.playback_control_revision,
            command=self._command, season=a._history_source_season,
            episode=a._history_source_episode, frame_evidence=frames.capture,
            clock=self.clock)
        admission = reader.capture()
        old = admission['snapshot']
        # Never reserve budget or contact a provider for paused/healthy/non-HLS.
        if (old['pause'] is not False or old['paused_for_cache'] is not True
                or old['seeking'] is not False or old['live'] is not False
                or admission['source'].get('kind') != 'hls'):
            return {'state':'ineligible','loaded':False}
        jid = admission['job']['job_id']
        app_path = Path(a._job_path(jid))
        write_path = Path(a._job_write_path(admission['job']))
        player_path = Path(p.JOBS) / (jid + '.json')
        if app_path != write_path:
            raise SnapshotUnavailable('canonical job write path changed')
        player_job = read_json(player_path)
        active_rows = [s for s in player_job.get('sources', [])
                       if isinstance(s,dict) and s.get('source_id') == old['source_id']]
        if (player_job.get('job_id') != jid or len(active_rows) != 1
                or canonical(active_rows[0]) != canonical(admission['source'])):
            raise SnapshotUnavailable('active player job disagrees with app job')
        expected_player = [deepcopy(player_job)]
        def read_job_checked():
            value = read_json(app_path)
            if canonical(read_json(player_path)) != canonical(expected_player[0]):
                raise SnapshotUnavailable('active player job changed')
            return value
        reader.job = lambda _: read_job_checked()
        post = MPVPostloadSnapshot(reader)
        stage = {'fresh':None,'seen':False,'sent':False,'ready':False}

        def snapshot():
            fresh = stage['fresh']
            if stage['sent'] and not stage['ready']:
                # loadfile is asynchronous. Missing properties are pending only
                # within the bounded initial load window and exact old ownership.
                post._fence(admission)
                try:
                    path = self._property('path')
                    if path not in (None, '', fresh['url'], admission['source']['url']):
                        raise SnapshotUnavailable('foreign loading path')
                    captured = (post.capture(admission,fresh) if path == fresh['url']
                                else reader.capture())['snapshot']
                except SnapshotUnavailable as error:
                    post._fence(admission)
                    pending = {'MPV property unavailable','MPV observation unavailable',
                        'selected track is unresolved','selected track is ambiguous',
                        'player is not loaded VOD','stream identity unavailable'}
                    if str(error) in pending:
                        raise RecoveryLoadPending() from None
                    raise
                if path != fresh['url']:
                    raise RecoveryLoadPending()
                stage['seen'] = True
                if captured['pause'] is True and (captured['seeking'] is True
                        or abs(captured['position']-old['position']) > 2):
                    raise RecoveryLoadPending()
                stage['ready'] = True
                return captured
            if fresh is not None and self._property('path') == fresh['url']:
                stage['seen'] = True
                return post.capture(admission, fresh)['snapshot']
            if stage['seen']:
                raise SnapshotUnavailable('loaded source changed again')
            return reader.capture()['snapshot']

        def recovery_command(parts, *, timeout, expected_revision):
            reply = self._command(parts, timeout=timeout, expected_revision=expected_revision)
            if parts[0] == 'loadfile' and reply.get('error') == 'success':
                stage['sent'] = True
            return reply

        def resolve(job, source, *, timeout):
            fresh = prepare_recovery_source(job, source,
                refresh=a._refresh_stale_source, resolver=self.resolver, timeout=timeout)
            # Real selector returns provider episode labels, while the snapshot
            # uses canonical episode identity. Normalize with the SAME helpers.
            fresh = deepcopy(fresh)
            fresh['season'] = a._history_source_season(job, fresh)
            fresh['episode'] = a._history_source_episode(job, fresh)
            if fresh['season'] != old['season'] or fresh['episode'] != old['episode']:
                raise SnapshotUnavailable('resolved episode changed')
            stage['fresh'] = fresh
            return fresh

        def restore(fresh, *, expected_revision):
            if expected_revision != admission['revision']:
                raise SnapshotUnavailable('different restoration revision')
            return post.restore_tracks(admission, fresh,
                tracks=lambda:self._property('track-list'), command=self._command,
                mutation_guard=self.mutation_guard)['snapshot']

        def write_job(staged):
            if (canonical(read_json(app_path)) != canonical(admission['job'])
                    or canonical(read_json(player_path)) != canonical(player_job)):
                raise SnapshotUnavailable('job changed before publication')
            mirrored = deepcopy(player_job)
            known = {s.get('source_id'):s for s in mirrored.get('sources', [])}
            for row in staged['sources']:
                if row.get('source_id') not in known:
                    mirrored['sources'].append(deepcopy(row))
                elif canonical(known[row['source_id']]) != canonical(row):
                    raise SnapshotUnavailable('player source collision')
            p._atomic_private(app_path, staged)
            if app_path != player_path:
                p._atomic_private(player_path, mirrored)
            expected_player[0] = deepcopy(staged if app_path == player_path else mirrored)
            if canonical(read_json(player_path)) != canonical(expected_player[0]):
                raise SnapshotUnavailable('player job publication unverified')

        commit = RecoveryCommit(admission=admission,
            read_job=lambda _:read_job_checked(), read_current=lambda:read_json(p.CURRENT),
            write_job=write_job, write_current=lambda v:p._atomic_private(p.CURRENT,v),
            conditional_commit=p._PLAYBACK_INTENT.commit,
            ownership=lambda:dict(endpoint='home_edge_01',mode=self.mode(),
                process=self.process(),revision=p.playback_control_revision()),
            snapshot=lambda fresh:post.capture(admission,fresh),
            write_last_vod=lambda job,source,position,paused:p._write_last_vod(
                job,source,position,self._property('duration'),paused,'hls-recovery'),
            read_last_vod=lambda:read_json(p.LAST_VOD))
        return self.engine.run(job=admission['job'],source=admission['source'],
            snapshot=snapshot, revision=p.playback_control_revision, resolve=resolve,
            command=recovery_command, mutation_guard=self.mutation_guard, commit=commit,
            clock=self.clock, sleep=self.sleep, transport_options=lambda source:
                self._transport(admission['job'],source), restore_tracks=restore,
            reserve_budget=lambda state,now:self.budget.reserve(state['session'],now))
