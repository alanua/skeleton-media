"""Authoritative MPV recovery snapshot; all adapters are explicit read-only calls.

No player startup, app import, polling thread or estimated frame counter.
"""
from copy import deepcopy
import hashlib
import json
import math
import re
import time


class SnapshotUnavailable(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)


def finite(value):
    if isinstance(value,bool):
        return None
    try:
        value=float(value)
    except (ValueError,TypeError,OverflowError):
        return None
    return value if math.isfinite(value) else None


def selected_track_identity(tracks, kind, selected):
    """An ID is useful only with one selected, unambiguous track descriptor."""
    if selected == 'no' or selected is False:
        return {'disabled':True,'type':kind}
    if type(selected) is not int or selected <= 0 or not isinstance(tracks,list):
        raise SnapshotUnavailable('selected track is unresolved')
    matching=[t for t in tracks if isinstance(t,dict) and t.get('type')==kind
              and t.get('id')==selected and t.get('selected') is True]
    if len(matching)!=1:
        raise SnapshotUnavailable('selected track is ambiguous')
    row=matching[0]
    # Numeric IDs can be reassigned after load. Keep exact content descriptors,
    # including external origin, for a later fail-closed remapping step.
    return {key:row.get(key) for key in
            ('type','title','lang','codec','external','external-filename','demux-id')}


class MPVRecoverySnapshot:
    def __init__(self, *, record, job, mode, process, revision, command,
                 season, episode, frame_evidence=None, clock=time.monotonic):
        self.record=record;self.job=job;self.mode=mode;self.process=process
        self.revision=revision;self.command=command;self.season=season
        self.episode=episode;self.frame_evidence=frame_evidence;self.clock=clock

    def capture(self, endpoint='home_edge_01'):
        started=self.clock()
        rev=self.revision()
        if endpoint!='home_edge_01' or type(rev) is not int or rev<0:
            raise SnapshotUnavailable('endpoint or intent is unavailable')
        if self.mode()!='mpv':
            raise SnapshotUnavailable('different active mode')
        process=self.process()
        # process = (boot ID, PID, /proc start ticks), never PID alone.
        if (not isinstance(process,tuple) or len(process)!=3
                or not process[0] or type(process[1]) is not int or process[1]<=0
                or not str(process[2]).isdigit()):
            raise SnapshotUnavailable('process identity unavailable')
        current=deepcopy(self.record())
        if not isinstance(current,dict):
            raise SnapshotUnavailable('invalid current record')
        job_id=current.get('job_id');source_id=current.get('source_id')
        if (not isinstance(job_id,str) or not re.fullmatch('[0-9a-f]{16}',job_id)
                or not isinstance(source_id,str) or not source_id
                or current.get('pid')!=process[1] or not current.get('started_at')):
            raise SnapshotUnavailable('current record does not own the player')
        job=deepcopy(self.job(job_id))
        if not isinstance(job,dict) or job.get('job_id')!=job_id:
            raise SnapshotUnavailable('current job identity mismatch')
        sources=[s for s in job.get('sources',[]) if isinstance(s,dict)
                 and s.get('source_id')==source_id]
        if len(sources)!=1:
            raise SnapshotUnavailable('current source is ambiguous')
        source=deepcopy(sources[0])
        values={}
        for prop in ('pause','time-pos','paused-for-cache','seeking','path',
                     'stream-open-filename','vo-configured','idle-active','eof-reached',
                     'sid','aid','sub-visibility','track-list'):
            remaining=3-(self.clock()-started)
            if remaining<=0:
                raise SnapshotUnavailable('snapshot deadline')
            result=self.command(['get_property',prop],timeout=min(.25,remaining))
            if not isinstance(result,dict) or result.get('error')!='success':
                raise SnapshotUnavailable('MPV property unavailable')
            values[prop]=result.get('data')
        path=values['path']
        if not isinstance(path,str) or not path.startswith(('http://','https://')):
            raise SnapshotUnavailable('stream identity unavailable')
        # Before a recovery load the exact old source URL must own MPV.
        if source.get('url') not in (path,values['stream-open-filename']):
            raise SnapshotUnavailable('stream and source record differ')
        position=finite(values['time-pos'])
        if (position is None or position<0 or values['vo-configured'] is not True
                or values['idle-active'] is not False or values['eof-reached'] is not False):
            raise SnapshotUnavailable('player is not loaded VOD')
        for field in ('pause','paused-for-cache','seeking','sub-visibility'):
            if type(values[field]) is not bool:
                raise SnapshotUnavailable('player state is unresolved')
        audio=selected_track_identity(values['track-list'],'audio',values['aid'])
        subtitles=selected_track_identity(values['track-list'],'sub',values['sid'])
        # Re-read user-controlled selections to detect direct IPC/key changes
        # that might have interleaved with property collection.
        for field in ('pause','aid','sid','sub-visibility'):
            remaining=3-(self.clock()-started)
            if remaining<=0:
                raise SnapshotUnavailable('snapshot deadline')
            check=self.command(['get_property',field],timeout=min(.25,remaining))
            if check.get('error')!='success' or check.get('data')!=values[field]:
                raise SnapshotUnavailable('selection changed during snapshot')
        if (self.clock()-started>3 or self.revision()!=rev or self.mode()!='mpv'
                or self.process()!=process or canonical(self.record())!=canonical(current)
                or canonical(self.job(job_id))!=canonical(job)):
            raise SnapshotUnavailable('session changed during snapshot')
        # Recovery publication changes source/snapshot/resume fields, not the
        # playback session. Counting those changes would reset the retry budget.
        stable={key:current.get(key) for key in
                ('pid','started_at','job_id','content_key','episode_key','identity_scope')}
        session=hashlib.sha256(canonical([process,stable]).encode()).hexdigest()
        frames=None
        # The actual collector is deliberately not invented. Clock/fps,
        # estimated-frame-number and drop counters cannot supply this evidence.
        if self.frame_evidence is not None:
            sample=self.frame_evidence()
            observed=finite(sample.get('observed_at')) if isinstance(sample,dict) else None
            count=finite(sample.get('frames')) if isinstance(sample,dict) else None
            if (isinstance(sample,dict) and sample.get('process')==process
                    and sample.get('path')==path
                    and observed is not None and started<=observed<=self.clock()
                    and count is not None and count>=0):
                frames=count
        if (self.clock()-started>3 or self.revision()!=rev or self.mode()!='mpv'
                or self.process()!=process or canonical(self.record())!=canonical(current)
                or canonical(self.job(job_id))!=canonical(job)):
            raise SnapshotUnavailable('session changed during frame evidence')
        return {
            'snapshot':dict(endpoint=endpoint,mode='mpv',session=session,
                job_id=job_id,source_id=source_id,season=self.season(job,source),
                episode=self.episode(job,source),
                voice=str(source.get('group') or source.get('translation') or ''),
                sid='no' if values['sid'] is False else values['sid'],
                aid='no' if values['aid'] is False else values['aid'],
                sub_visibility=values['sub-visibility'],
                position=position,pause=values['pause'],seeking=values['seeking'],
                paused_for_cache=values['paused-for-cache'],live=bool(source.get('live')),
                frames=frames,path=path,loaded=True),
            'revision':rev,'job':job,'source':source,'current':current,
            'process':process,'audio_identity':audio,'subtitle_identity':subtitles,
            'job_sha256':hashlib.sha256(canonical(job).encode()).hexdigest(),
            'current_sha256':hashlib.sha256(canonical(current).encode()).hexdigest(),
        }
