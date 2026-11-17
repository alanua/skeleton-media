"""Apply the reviewed observation patch to an exact isolated source snapshot."""
import argparse,hashlib,json
from pathlib import Path
PLAN={
  "player.py": {
    "before_sha256": "f6a9c771858e5067c3e09fef93cc8c5357b4d6edfa8ebd2e3ec98787dacfc754",
    "after_sha256": "cde34321422e5365f0c615a46f08ab55c6156cd2acb0d1bac25f8135f4431cd8",
    "edits": [
      [
        "            out[prop] = command(['get_property', prop]).get('data')\n        except Exception:\n            out[prop] = None\n    try:\n        tracks = command(['get_property', 'track-list']).get('data') or []\n    except Exception:\n",
        "            out[prop] = command(['get_property', prop]).get('data')\n        except Exception:\n            out[prop] = None\n    # Extra observation IPC is bounded; cache payloads expose scalar counters only.\n    deadline = time.monotonic() + 1.0\n    for prop in ('paused-for-cache', 'demuxer-cache-state', 'cache-buffering-state',\n                 'seeking', 'eof-reached', 'estimated-frame-number',\n                 'decoder-frame-drop-count', 'frame-drop-count', 'speed'):\n        remaining = deadline - time.monotonic()\n        try:\n            value = command(['get_property', prop], timeout=min(0.15, remaining)).get('data') if remaining > 0 else None\n            if prop == 'demuxer-cache-state':\n                value = {k: value.get(k) for k in ('cache-duration', 'fw-bytes', 'total-bytes', 'reader-pts', 'cache-end', 'raw-input-rate', 'eof', 'underrun')} if isinstance(value, dict) else None\n            out[prop] = value\n        except Exception:\n            out[prop] = None\n    out.update({key: metadata.get(key) for key in ('session_id', 'pid', 'started_at')})\n    out['observation_mode'] = 'mpv'\n    out['mpv_observed_at'] = time.time()\n    # MPV's estimated frame number and drop counters are not decoded totals.\n    # Leave decoded_frames absent when the backend cannot provide that evidence.\n    try:\n        tracks = command(['get_property', 'track-list']).get('data') or []\n    except Exception:\n"
      ],
      [
        "                        'external', 'audio-channels', 'demux-channels', 'demux-samplerate')\n            if track.get(key) is not None\n        }\n    out['audio_tracks'] = [public_track(track) for track in tracks if isinstance(track, dict) and track.get('type') == 'audio']\n    out['subtitle_tracks'] = [public_track(track) for track in tracks if isinstance(track, dict) and track.get('type') == 'sub']\n    return out\n",
        "                        'external', 'audio-channels', 'demux-channels', 'demux-samplerate')\n            if track.get(key) is not None\n        }\n    out['audio_only'] = bool(tracks) and any(isinstance(t, dict) and t.get('type') == 'audio' and t.get('selected') is True for t in tracks) and not any(isinstance(t, dict) and t.get('type') == 'video' and t.get('selected') is True for t in tracks)\n    out['audio_tracks'] = [public_track(track) for track in tracks if isinstance(track, dict) and track.get('type') == 'audio']\n    out['subtitle_tracks'] = [public_track(track) for track in tracks if isinstance(track, dict) and track.get('type') == 'sub']\n    return out\n"
      ]
    ]
  },
  "media_health.py": {
    "before_sha256": "e27ab95bc7c4b5c5e413345e1943acf82a3959727ded73a96435c89866cbf6e4",
    "after_sha256": "5a27ae799ba320208c97e0b94ef971e6e5d415d0c97902887a81a8e0acec0da1",
    "edits": [
      [
        "HOME = Path.home()\nsys.path.insert(0,str(HOME/'.local/lib/skeleton-cast'))\nfrom youtube_playback_health import youtube_progress_health\nCONFIG_PATH = HOME / '.config/skeleton/media-platform/health.json'\nSTATE_DIR = HOME / '.local/state/skeleton/media-health'\nSTATE_FILE = STATE_DIR / 'state.json'\n",
        "HOME = Path.home()\nsys.path.insert(0,str(HOME/'.local/lib/skeleton-cast'))\nfrom youtube_playback_health import youtube_progress_health\nfrom mpv_playback_health import mpv_progress_health, finite_number\nCONFIG_PATH = HOME / '.config/skeleton/media-platform/health.json'\nSTATE_DIR = HOME / '.local/state/skeleton/media-health'\nSTATE_FILE = STATE_DIR / 'state.json'\n"
      ],
      [
        "    host = ''\n    try: host = urlsplit(raw_url).hostname or ''\n    except Exception: pass\n    return {\n        'running': bool(player.get('running')),\n        'backend': player.get('backend'),\n",
        "    host = ''\n    try: host = urlsplit(raw_url).hostname or ''\n    except Exception: pass\n    from mpv_playback_health import finite_number\n    cache = player.get('demuxer-cache-state')\n    cache = cache if isinstance(cache, dict) else {}\n    def number(*keys):\n        for key in keys:\n            if key in player:\n                return finite_number(player[key])\n        return None\n    return {\n        'running': bool(player.get('running')),\n        'backend': player.get('backend'),\n"
      ],
      [
        "        'profile': player.get('profile'),\n        'pause': player.get('pause'),\n        'buffering': bool(player.get('buffering')),\n        'time_pos': float(player.get('time-pos') or player.get('time_pos') or 0),\n        'duration': float(player.get('duration') or 0),\n        'ready_state': int(player.get('ready_state') or 0),\n        'quality': player.get('quality'),\n        'fullscreen': player.get('fullscreen'),\n        'error': player.get('error'),\n        'aid': player.get('aid'),\n        **{key:player.get(key) for key in ('decoded_frames','video_width','video_height','playback_rate','seeking','ended','audio_only')},\n    }\n\n\n",
        "        'profile': player.get('profile'),\n        'pause': player.get('pause'),\n        'buffering': bool(player.get('buffering')),\n        'time_pos': number('time-pos', 'time_pos'),\n        'duration': number('duration'),\n        'ready_state': number('ready_state'),\n        'quality': player.get('quality'),\n        'fullscreen': player.get('fullscreen'),\n        'error': player.get('error'),\n        'aid': player.get('aid'),\n        **{key:player.get(key) for key in ('seeking', 'audio_only', 'observation_mode',\n            'job_id', 'season', 'episode', 'session_id', 'pid', 'started_at')},\n        **{key:number(key) for key in ('decoded_frames', 'decoder_frames', 'video_width', 'video_height', 'mpv_observed_at')},\n        'playback_rate': number('speed', 'playback_rate'),\n        'ended': player.get('eof-reached', player.get('ended')),\n        'paused_for_cache': player.get('paused-for-cache', player.get('paused_for_cache')),\n        'cache_duration': finite_number(cache.get('cache-duration', player.get('cache_duration'))),\n        'cache_buffering_state': number('cache-buffering-state', 'cache_buffering_state'),\n        'cache_counters': {k: finite_number(cache.get(k)) for k in\n            ('fw-bytes', 'total-bytes', 'reader-pts', 'cache-end', 'raw-input-rate')},\n        'estimated_frame_number': number('estimated-frame-number'),\n        'dropped_frames': number('frame-drop-count'),\n        'decoder_dropped_frames': number('decoder-frame-drop-count'),\n    }\n\n\n"
      ],
      [
        "        progress_state=playback_progress['state']\n        severity='ok' if playback_progress['healthy'] else ('info' if progress_state in {'paused','stopped'} else 'warning')\n        add('playback_progress',severity,'YouTube playback '+progress_state,playback_progress)\n\n    critical = [c for c in checks if c['status'] == 'critical']\n    warnings = [c for c in checks if c['status'] == 'warning']\n",
        "        progress_state=playback_progress['state']\n        severity='ok' if playback_progress['healthy'] else ('info' if progress_state in {'paused','stopped'} else 'warning')\n        add('playback_progress',severity,'YouTube playback '+progress_state,playback_progress)\n\n    elif active_mode == 'mpv':\n        previous_snapshot = read_json(LAST_FILE, {})\n        previous_player = previous_snapshot.get('player') if previous_snapshot.get('mode') == 'mpv' and not previous_snapshot.get('transition_active') else {}\n        try:\n            elapsed = now - float(previous_snapshot.get('checked_at') or 0)\n        except (TypeError, ValueError, OverflowError):\n            elapsed = 0\n        playback_progress = mpv_progress_health(previous_player, player, elapsed)\n        observed_at = finite_number(player.get('mpv_observed_at'))\n        if transition:\n            playback_progress = {'state': 'reset', 'healthy': False}\n        elif observed_at is None or not 0 <= now - observed_at <= 5:\n            playback_progress = {'state': 'stale', 'healthy': False}\n        progress_state = playback_progress['state']\n        severity = 'ok' if playback_progress['healthy'] else ('info' if progress_state in {'paused', 'ended', 'seeking', 'stopped', 'reset'} else 'warning')\n        add('mpv_playback_progress', severity, 'MPV playback ' + progress_state, playback_progress)\n\n    critical = [c for c in checks if c['status'] == 'critical']\n    warnings = [c for c in checks if c['status'] == 'warning']\n"
      ]
    ]
  }
}
def apply(root):
 prepared=[]
 for name,spec in PLAN.items():
  p=root/name;data=p.read_bytes();sha=hashlib.sha256(data).hexdigest()
  if sha==spec["after_sha256"]:continue
  if sha!=spec["before_sha256"]:raise SystemExit("source identity mismatch: "+name)
  text=data.decode()
  for old,new in spec["edits"]:
   if text.count(old)!=1:raise SystemExit("ambiguous boundary: "+name)
   text=text.replace(old,new,1)
  data=text.encode()
  if hashlib.sha256(data).hexdigest()!=spec["after_sha256"]:raise SystemExit("result identity mismatch: "+name)
  prepared.append((p,data))
 for p,data in prepared:p.write_bytes(data)
 return len(prepared)
if __name__=="__main__":
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("isolated_backend",type=Path)
 args=parser.parse_args();print(json.dumps({"changed_files":apply(args.isolated_backend),"runtime_applied":False,"published":False}))
