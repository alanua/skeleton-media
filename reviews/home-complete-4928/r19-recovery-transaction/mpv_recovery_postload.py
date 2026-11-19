"""Read authoritative post-load state and restore semantic track selections.

The durable job/current records remain unchanged until the recovery transaction
commits. A detached transitional view permits exactly the resolved URL; original
records, process, endpoint and control revision are fenced before and after reads.
No playback startup, resume, worker, persistence or estimated frame counter.
"""
from copy import deepcopy
from mpv_recovery_snapshot import (
    MPVRecoverySnapshot, SnapshotUnavailable, canonical, finite,
)

TRACK_FIELDS = ('type', 'title', 'lang', 'codec', 'external', 'external-filename')


def track_signature(track):
    return tuple(track.get(key) for key in TRACK_FIELDS)



def track_selection_guard(tracks):
    if not isinstance(tracks,list):
        raise SnapshotUnavailable('track list unavailable')
    # MPV fills unrelated video dimensions/decoder metadata asynchronously.
    # Fence every selectable audio/subtitle descriptor and actual selected flag.
    rows=[{key:t.get(key) for key in TRACK_FIELDS+('id','selected')}
          for t in tracks if isinstance(t,dict) and t.get('type') in ('audio','sub')]
    return canonical(sorted(rows,key=canonical))


def remap_track(identity, tracks):
    if not isinstance(identity, dict) or identity.get('type') not in ('audio', 'sub'):
        raise SnapshotUnavailable('track identity unavailable')
    if identity.get('disabled') is True:
        return 'no'
    if not isinstance(tracks, list):
        raise SnapshotUnavailable('track list unavailable')
    # A demux ID or MPV numeric ID can be reassigned by a newly resolved stream.
    # Require one exact semantic descriptor; never guess among same-language tracks.
    matches = [track for track in tracks if isinstance(track, dict)
               and track_signature(track) == track_signature(identity)
               and type(track.get('id')) is int and track['id'] > 0]
    if len(matches) != 1:
        raise SnapshotUnavailable('track is missing or ambiguous')
    return matches[0]['id']


class MPVPostloadSnapshot:
    def __init__(self, reader):
        self.reader = reader

    def _fence(self, admission):
        r = self.reader
        if (not isinstance(admission, dict) or r.mode() != 'mpv'
                or r.revision() != admission.get('revision')
                or r.process() != admission.get('process')
                or canonical(r.record()) != canonical(admission.get('current'))
                or canonical(r.job(admission['job']['job_id'])) != canonical(admission['job'])):
            raise SnapshotUnavailable('recovery ownership changed')

    def capture(self, admission, fresh):
        self._fence(admission)
        old = admission['snapshot']
        url = fresh.get('url') if isinstance(fresh, dict) else None
        if (not isinstance(url, str) or not url.startswith(('http://', 'https://'))
                or fresh.get('drm') or fresh.get('has_drm') or fresh.get('live')
                or fresh.get('is_trailer')
                or fresh.get('season', old['season']) != old['season']
                or fresh.get('episode', old['episode']) != old['episode']
                or str(fresh.get('group') or fresh.get('translation') or '') != old['voice']):
            raise SnapshotUnavailable('fresh source does not preserve selection')
        # This is a detached transitional read, never a durable source rewrite.
        projected = deepcopy(admission['job'])
        found = [s for s in projected['sources']
                 if s.get('source_id') == old['source_id']]
        if len(found) != 1:
            raise SnapshotUnavailable('ambiguous admission source')
        found[0]['url'] = url
        r = self.reader
        def projected_job(job_id):
            self._fence(admission)
            if job_id != admission['job']['job_id']:
                raise SnapshotUnavailable('different job requested')
            return deepcopy(projected)
        reader = MPVRecoverySnapshot(
            record=r.record, job=projected_job, mode=r.mode, process=r.process,
            revision=r.revision, command=r.command, season=r.season,
            episode=r.episode, frame_evidence=r.frame_evidence, clock=r.clock)
        captured = reader.capture()
        self._fence(admission)
        if (captured['snapshot']['session'] != old['session']
                or captured['snapshot']['path'] != url):
            raise SnapshotUnavailable('different post-load session or path')
        # Preserve authoritative durable provenance instead of leaking the read view.
        captured['job'] = deepcopy(admission['job'])
        captured['source'] = deepcopy(admission['source'])
        captured['job_sha256'] = admission['job_sha256']
        captured['staged_source'] = deepcopy(fresh)
        return captured

    def restore_tracks(self, admission, fresh, *, tracks, command, mutation_guard):
        """Restore while paused; caller retains sole responsibility for resume."""
        r = self.reader
        with mutation_guard():
            self._fence(admission)
            before = self.capture(admission, fresh)
            old = admission['snapshot']
            state = before['snapshot']
            if (state['pause'] is not True or state['seeking'] is not False
                    or finite(state['position']) is None
                    or abs(state['position'] - old['position']) > 2):
                raise SnapshotUnavailable('post-load preservation is unavailable')
            original_tracks = deepcopy(tracks())
            desired = {
                'aid': remap_track(admission['audio_identity'], original_tracks),
                'sid': remap_track(admission['subtitle_identity'], original_tracks),
                'sub-visibility': old['sub_visibility'],
            }
            expected = {key: state[key] for key in ('aid', 'sid', 'sub_visibility', 'pause')}
            for prop, target in desired.items():
                self._fence(admission)
                current = self.capture(admission, fresh)['snapshot']
                if (any(current[k] != expected[k] for k in expected)
                        or abs(current['position'] - old['position']) > 2
                        or current['seeking'] is not False
                        or track_selection_guard(tracks()) != track_selection_guard(original_tracks)):
                    raise SnapshotUnavailable('selection changed before restoration')
                key = 'sub_visibility' if prop == 'sub-visibility' else prop
                if current[key] == target:
                    continue
                result = command(['set_property', prop, target],
                                 timeout=2, expected_revision=admission['revision'])
                if not isinstance(result, dict) or result.get('error') != 'success':
                    raise SnapshotUnavailable('track restoration rejected')
                expected[key] = target
                # MPV's selected flags can change after each accepted selection.
                original_tracks = deepcopy(tracks())
            final = self.capture(admission, fresh)
            state = final['snapshot']
            if (state['aid'] != desired['aid'] or state['sid'] != desired['sid']
                    or state['sub_visibility'] != desired['sub-visibility']
                    or state['pause'] is not True or state['seeking'] is not False
                    or abs(state['position'] - old['position']) > 2
                    or track_signature(final['audio_identity']) != track_signature(admission['audio_identity'])
                    or (not admission['subtitle_identity'].get('disabled')
                        and track_signature(final['subtitle_identity']) != track_signature(admission['subtitle_identity']))):
                raise SnapshotUnavailable('restored tracks not independently verified')
            self._fence(admission)
            return final
