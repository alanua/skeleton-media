"""Pure, conservative MPV observations; no transport or repair operations."""
import math


def finite_number(value):
    """MPV JSON numbers only: booleans, strings and non-finite values are invalid."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def mpv_progress_health(previous, current, elapsed):
    def result(state, healthy=False, **evidence):
        return dict(state=state, healthy=healthy, **evidence)

    if not isinstance(current, dict):
        return result('unknown')
    if current.get('observation_mode') != 'mpv':
        return result('not_applicable')
    if current.get('pause') is True:
        return result('paused')
    if current.get('ended') is True:
        return result('ended')
    if current.get('seeking') is True:
        return result('seeking')
    if current.get('running') is not True:
        return result('stopped')
    if not isinstance(previous, dict) or not previous:
        return result('sample')
    keys = ('observation_mode', 'backend', 'source_id', 'video_id', 'channel_id',
            'job_id', 'season', 'episode', 'session_id', 'pid', 'started_at')
    for sample in (previous, current):
        for key in keys:
            value = sample.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float)) or (isinstance(value, (int, float)) and finite_number(value) is None)):
                return result('unknown')
    if any(previous.get(k) != current.get(k) for k in keys):
        return result('reset')
    # Both content and player-instance evidence are necessary to compare windows.
    if not any(current.get(k) for k in ('source_id', 'video_id', 'channel_id')):
        return result('unknown')
    if not any(current.get(k) for k in ('session_id', 'pid', 'started_at')):
        return result('unknown')
    for sample in (previous, current):
        if (sample.get('running') is not True or sample.get('pause') is not False
                or sample.get('seeking') is not False or sample.get('ended') is not False):
            return result('sample')
    duration = finite_number(elapsed)
    before_at = finite_number(previous.get('mpv_observed_at'))
    after_at = finite_number(current.get('mpv_observed_at'))
    if duration is None or not 3 <= duration <= 90:
        return result('stale')
    if before_at is None or after_at is None:
        return result('unknown')
    span = after_at - before_at
    if not 3 <= span <= 90 or abs(span - duration) > 2:
        return result('stale')
    old = finite_number(previous.get('time_pos'))
    new = finite_number(current.get('time_pos'))
    if old is None or new is None or min(old, new) < 0:
        return result('unknown')
    delta = new - old
    rate = finite_number(current.get('playback_rate'))
    if delta < -0.05 or delta > span * max(rate or 1, 1) * 2 + 1:
        return result('reset')
    if current.get('playback_rate') is not None and (rate is None or rate <= 0):
        return result('unknown')
    frames = []
    for key in ('decoded_frames', 'decoder_frames'):
        a, b = finite_number(previous.get(key)), finite_number(current.get(key))
        if any(s.get(key) is not None and finite_number(s.get(key)) is None for s in (previous, current)):
            return result('unknown')
        if a is not None and b is not None:
            if min(a, b) < 0 or a != int(a) or b != int(b):
                return result('unknown')
            if b < a:
                return result('reset')
            frames.append(b - a)
    evidence = {'clock_delta': delta, 'frame_delta': frames[0] if frames else None}
    buffering = current.get('buffering') is True or current.get('paused_for_cache') is True
    if delta > 0.05:
        if buffering:
            return result('buffering_recovery', **evidence)
        if frames and any(n == 0 for n in frames):
            return result('frame_stalled', **evidence)
        if frames or current.get('audio_only') is True:
            return result('playing', True, **evidence)
        return result('progressing_unverified', **evidence)
    # Ten seconds of comparable flat clock; filling cache alone is not playback.
    if span < 10:
        return result('sample', **evidence)
    if frames and any(n > 0 for n in frames):
        return result('unknown', **evidence)
    def empty(sample):
        cache = finite_number(sample.get('cache_duration'))
        return (sample.get('paused_for_cache') is True and cache is not None
                and 0 <= cache <= 0.05)
    if empty(previous) and empty(current):
        return result('cache_starved', **evidence)
    if buffering:
        return result('buffering', **evidence)
    return result('decode_stalled' if frames else 'stalled_unknown', **evidence)
