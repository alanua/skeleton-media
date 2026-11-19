"""Exact R19-baseline app wiring for the serialized incoming receiver callback."""
import ast
import hashlib

APP_BEFORE='529cf5fea68699f4db4fec166d4a6a8185a82609df4039918bc3a88df3b76ffe'
APP_HELPER="\n_INCOMING_YOUTUBE_RUNTIME = None\n_INCOMING_YOUTUBE_NEXT_RETRY = 0.0\n_INCOMING_YOUTUBE_FAILURES = 0\n_INCOMING_YOUTUBE_LAST_RESULT = None\n\ndef _incoming_youtube_tick() -> None:\n    \"\"\"Serial callback in the EXISTING autoplay poll monitor.\"\"\"\n    global _INCOMING_YOUTUBE_RUNTIME, _INCOMING_YOUTUBE_NEXT_RETRY\n    global _INCOMING_YOUTUBE_FAILURES, _INCOMING_YOUTUBE_LAST_RESULT\n    now = time.monotonic()\n    if now < _INCOMING_YOUTUBE_NEXT_RETRY:\n        return\n    try:\n        if _INCOMING_YOUTUBE_RUNTIME is None:\n            import sys\n            from youtube_incoming_runtime import IncomingYouTubeRuntime\n            _INCOMING_YOUTUBE_RUNTIME = IncomingYouTubeRuntime(\n                app=sys.modules[__name__], player=player,\n                root=HOME / '.local/state/skeleton-cast/incoming-youtube',\n                actuate=True)\n        result = _INCOMING_YOUTUBE_RUNTIME.tick()\n        if result.get('state') == 'observer_unavailable':\n            _INCOMING_YOUTUBE_FAILURES = min(5,_INCOMING_YOUTUBE_FAILURES+1)\n            _INCOMING_YOUTUBE_NEXT_RETRY = time.monotonic()+min(30,2**_INCOMING_YOUTUBE_FAILURES)\n        else:\n            _INCOMING_YOUTUBE_FAILURES = 0\n        public = {k:result[k] for k in ('state','sent','published','phase','error_type') if k in result}\n        if public != _INCOMING_YOUTUBE_LAST_RESULT:\n            player._atomic_private(_INCOMING_YOUTUBE_RUNTIME.root / 'last-result.json', {\n                'schema':'skeleton.incoming-youtube-result.v1',\n                'checked_at':int(time.time()), **public,\n            })\n            _INCOMING_YOUTUBE_LAST_RESULT = public\n    except Exception:\n        _INCOMING_YOUTUBE_NEXT_RETRY = time.monotonic()+5\n\n"

def patch_app(source):
    if hashlib.sha256(source.encode()).hexdigest()!=APP_BEFORE:
        raise ValueError('Current R19 app source changed; refuse incoming patch')
    tree=ast.parse(source)
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef)
              and n.name=='_video_autoplay_poll_monitor')
    before=ast.get_source_segment(source,node)
    marker='        _AUTOPLAY_MONITOR_HEARTBEAT = time.time()\n'
    if before.count(marker)!=1:raise ValueError('Existing monitor boundary changed')
    after=before.replace(marker,marker+
        '        try:\n            _incoming_youtube_tick()\n        except Exception:\n            pass\n',1)
    if source.count(before)!=1:raise ValueError('Ambiguous monitor source')
    source=source.replace(before,after,1)
    startup="\nif __name__ == '__main__':\n"
    if source.count(startup)!=1:raise ValueError('Startup boundary changed')
    source=source.replace(startup,'\n'+APP_HELPER+startup,1)
    ast.parse(source)
    return source
