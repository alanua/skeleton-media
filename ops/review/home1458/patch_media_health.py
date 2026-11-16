"""Add progress reporting to the existing watchdog without new repair actions."""
from pathlib import Path
from patch_backend import replace_once


def patch(path):
    p=Path(path);s=p.read_text()
    s=replace_once(s,'HOME = Path.home()',"HOME = Path.home()\nsys.path.insert(0,str(HOME/'.local/lib/skeleton-cast'))\nfrom youtube_playback_health import youtube_progress_health")
    s=replace_once(s,"        'aid': player.get('aid'),\n    }","        'aid': player.get('aid'),\n        **{key:player.get(key) for key in ('decoded_frames','video_width','video_height','playback_rate','seeking','ended','audio_only')},\n    }")
    marker="    critical = [c for c in checks if c['status'] == 'critical']"
    code='''    playback_progress={'state':'not_applicable','healthy':False}
    if active_mode=='kiosk':
        previous_snapshot=read_json(LAST_FILE,{})
        previous_player=previous_snapshot.get('player') if previous_snapshot.get('mode')=='kiosk' else {}
        try:elapsed=now-float(previous_snapshot.get('checked_at') or 0)
        except (TypeError,ValueError):elapsed=0
        playback_progress=youtube_progress_health(previous_player,player,elapsed)
        progress_state=playback_progress['state']
        severity='ok' if playback_progress['healthy'] else ('info' if progress_state in {'paused','stopped'} else 'warning')
        add('playback_progress',severity,'YouTube playback '+progress_state,playback_progress)

'''
    s=replace_once(s,marker,code+marker)
    s=replace_once(s,"        'player': player,\n        'services': service_values,","        'player': player,\n        'playback_progress': playback_progress,\n        'services': service_values,")
    p.write_text(s)


if __name__=='__main__':
    import sys
    patch(sys.argv[1])
