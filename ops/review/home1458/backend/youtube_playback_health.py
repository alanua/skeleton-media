"""Progress evidence for YouTube; page liveness alone is never playback health."""
import math
import re


def canonical_media_mode(status):
    if not isinstance(status,str):return 'unknown'
    modes=set(re.findall(r'(?m)^(?:TV_)?MODE=([a-z]+)$',status))
    if len(modes)!=1:return 'unknown'
    mode=next(iter(modes))
    return mode if mode in {'kiosk','chrome','mpv','tv','games','off'} else 'unknown'


def _number(value):
    if isinstance(value,bool):return None
    try:value=float(value)
    except (TypeError,ValueError,OverflowError):return None
    return value if math.isfinite(value) else None


def youtube_progress_health(before,after,elapsed):
    def result(state,healthy=False):return {'state':state,'healthy':healthy}
    if not isinstance(before,dict) or not isinstance(after,dict):return result('unverified')
    if not after.get('running') or after.get('ended'):return result('stopped')
    if after.get('pause') is True:return result('paused')
    if before.get('video_id')!=after.get('video_id') or not after.get('video_id'):return result('transition')
    if before.get('pause') is True or before.get('seeking') or after.get('seeking'):return result('transition')
    duration=_number(elapsed)
    if duration is None or duration<3:return result('unverified')
    old_time=_number(before.get('time-pos'));new_time=_number(after.get('time-pos'))
    if old_time is None or new_time is None:return result('unverified')
    advance=new_time-old_time
    rate=_number(after.get('playback_rate'))
    if advance<-.05 or (rate is not None and advance>duration*max(rate,1)*2+1):return result('transition')
    old_frames=_number(before.get('decoded_frames'));new_frames=_number(after.get('decoded_frames'))
    has_video=(_number(after.get('video_width')) or 0)>0
    if old_frames is not None and new_frames is not None and new_frames<old_frames:return result('transition')
    frame_advance=None if old_frames is None or new_frames is None else new_frames-old_frames
    if advance>.05 and (not has_video or (frame_advance is not None and frame_advance>0)):return result('playing',True)
    ready=_number(after.get('ready_state'))
    if after.get('buffering') or (ready is not None and ready<3):return result('buffering')
    if advance>.05 and has_video and frame_advance==0:return result('frame_stalled')
    if advance<=.05 and frame_advance==0:return result('stalled')
    return result('unverified')
