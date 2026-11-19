"""Revision-conditional additive publication after independently verified recovery.

Old sources remain valid throughout publication. The current record carries a
self-contained source snapshot, matching the existing player's fallback reader.
Writers are the canonical private atomic writers supplied by the runtime adapter.
No ordinary play, history membership edits, deletes or automatic rollback writes.
"""
from copy import deepcopy
import hashlib
from mpv_recovery_snapshot import SnapshotUnavailable, canonical, finite
from mpv_recovery_postload import track_signature


def selected_matches(expected, actual, selected):
    if expected.get('disabled') is True:
        return actual.get('disabled') is True and (selected == 'no' or selected is False)
    return (actual.get('disabled') is not True
            and track_signature(actual) == track_signature(expected))


class RecoveryCommitUnavailable(RuntimeError):
    pass


class RecoveryCommit:
    def __init__(self, *, admission, read_job, read_current, write_job,
                 write_current, conditional_commit, ownership, snapshot,
                 write_last_vod, read_last_vod):
        self.admission = deepcopy(admission)
        self.read_job = read_job
        self.read_current = read_current
        self.write_job = write_job
        self.write_current = write_current
        self.conditional_commit = conditional_commit
        self.ownership = ownership
        self.snapshot = snapshot
        self.write_last_vod = write_last_vod
        self.read_last_vod = read_last_vod

    def _fence(self, expected_revision, job, current):
        a = self.admission
        if (type(expected_revision) is not int or expected_revision != a['revision']
                or self.ownership() != {'endpoint':'home_edge_01', 'mode':'mpv',
                    'process':a['process'], 'revision':expected_revision}
                or canonical(self.read_job(a['job']['job_id'])) != canonical(job)
                or canonical(self.read_current()) != canonical(current)):
            raise RecoveryCommitUnavailable('recovery publication ownership changed')

    def __call__(self, prepared_job, fresh, *, expected_revision):
        a = self.admission
        if prepared_job.get('job_id') != a['job']['job_id']:
            raise RecoveryCommitUnavailable('different prepared job')
        old = a['snapshot']
        if (not isinstance(fresh, dict) or not fresh.get('url')
                or not str(fresh['url']).startswith(('http://','https://'))
                or fresh.get('drm') or fresh.get('has_drm') or fresh.get('live')
                or fresh.get('is_trailer')
                or fresh.get('season', old['season']) != old['season']
                or fresh.get('episode', old['episode']) != old['episode']
                or str(fresh.get('group') or fresh.get('translation') or '') != old['voice']):
            raise RecoveryCommitUnavailable('different prepared selection')

        def apply():
            self._fence(expected_revision, a['job'], a['current'])
            observed = self.snapshot(fresh)
            state = observed['snapshot']
            position = finite(state.get('position'))
            keys = ('endpoint','mode','session','job_id','source_id','season','episode','voice')
            if (any(state.get(k) != old.get(k) for k in keys)
                    or state.get('path') != fresh['url'] or state.get('loaded') is not True
                    or state.get('pause') is not False or state.get('seeking') is not False
                    or state.get('paused_for_cache') is not False
                    or position is None or not old['position']-2 <= position <= old['position']+30
                    or not selected_matches(a['audio_identity'], observed['audio_identity'], state['aid'])
                    or not selected_matches(a['subtitle_identity'], observed['subtitle_identity'], state['sid'])
                    or state.get('sub_visibility') != old['sub_visibility']):
                raise RecoveryCommitUnavailable('recovered selection is not verified')
            source = deepcopy(fresh)
            sid = source.get('source_id')
            if not isinstance(sid, str) or not sid or sid == old['source_id']:
                sid = 'recovery-' + hashlib.sha256(canonical(source).encode()).hexdigest()[:16]
            source['source_id'] = sid
            staged = deepcopy(a['job'])
            rows = staged.get('sources')
            if not isinstance(rows, list):
                raise RecoveryCommitUnavailable('source list unavailable')
            old_rows = [x for x in rows if x.get('source_id') == old['source_id']]
            if len(old_rows) != 1 or canonical(old_rows[0]) != canonical(a['source']):
                raise RecoveryCommitUnavailable('old source is not uniquely owned')
            matches = [x for x in rows if x.get('source_id') == sid]
            if matches:
                if len(matches) != 1 or canonical(matches[0]) != canonical(source):
                    raise RecoveryCommitUnavailable('fresh source identifier collides')
            else:
                rows.append(source)
            current = deepcopy(a['current'])
            current.update(source_id=sid, source_snapshot=deepcopy(source),
                           quality=source.get('quality'),
                           resume_from_seconds=round(position,3))
            if not current.get('translation'):
                current['translation'] = old['voice']
            current['recovery_selection'] = {
                'audio': deepcopy(a['audio_identity']),
                'subtitles': deepcopy(a['subtitle_identity']),
                'aid':state['aid'], 'sid':state['sid'],
                'sub_visibility':state['sub_visibility'],
            }
            # Stage the candidate additively: a crash leaves the old current
            # pointer and old source valid, rather than pointing at a missing row.
            self.write_job(deepcopy(staged))
            self._fence(expected_revision, staged, a['current'])
            self.write_current(deepcopy(current))
            self._fence(expected_revision, staged, current)
            self.write_last_vod(deepcopy(staged), deepcopy(source), position, False)
            self._fence(expected_revision, staged, current)
            last = self.read_last_vod()
            if (not isinstance(last, dict) or last.get('job_id') != staged['job_id']
                    or last.get('source_id') != sid or last.get('paused') is not False
                    or finite(last.get('position_seconds')) is None
                    or abs(last['position_seconds'] - position) > .01
                    or (current.get('content_key') and last.get('content_key') != current['content_key'])):
                raise RecoveryCommitUnavailable('last VOD publication is not verified')
            return {'committed':True,'job_id':staged['job_id'],'source_id':sid,
                    'old_source_retained':True,'current_session_preserved':True}

        return self.conditional_commit(expected_revision, apply)
