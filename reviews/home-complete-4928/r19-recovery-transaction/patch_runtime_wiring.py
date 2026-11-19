"""Exact reviewed-source wiring. Does not deploy or start processes."""
import ast
import hashlib

APP_BEFORE = '2d6c9a00d057cb2e1bfaa7012735b7231697f8e0a5d2562c5fbb93129a7698fc'
PLAYER_BEFORE = 'c6197c05b3ebfcc5fc6b91efe7dc3326d9d7ab8804ed2a177bd36d1b40c82236'
APP_HELPER = "\n_HLS_RECOVERY_RUNTIME = None\n\ndef _hls_recovery_tick() -> None:\n    \"\"\"Called serially by the EXISTING progress monitor; no extra worker.\"\"\"\n    global _HLS_RECOVERY_RUNTIME\n    # Cheap mode check also protects healthy browser/TV/game sessions.\n    if player._current_tv_mode() != 'mpv':\n        return\n    try:\n        if _HLS_RECOVERY_RUNTIME is None:\n            import sys\n            from mpv_recovery_runtime import MPVRecoveryRuntime\n            _HLS_RECOVERY_RUNTIME = MPVRecoveryRuntime(\n                app=sys.modules[__name__], player=player,\n                resolver_worker=Path(__file__).with_name('hls_resolver_request.py'),\n                root=HOME / '.local/state/skeleton-cast/hls-recovery')\n        result = _HLS_RECOVERY_RUNTIME.run_once()\n        if result.get('state') not in {'ineligible', 'busy'}:\n            # Only public-safe state, no signed source URLs or provider errors.\n            player._atomic_private(_HLS_RECOVERY_RUNTIME.root / 'last-result.json', {\n                'schema':'skeleton.hls-recovery-result.v1',\n                'checked_at':int(time.time()),\n                **{k:result[k] for k in ('state','loaded','attempt') if k in result},\n            })\n    except Exception:\n        # A recovery failure must not stop ordinary progress persistence.\n        pass\n\n"
PLAYER_HELPER = "\n_HLS_RECOVERY_TICK = None\n\ndef configure_hls_recovery_tick(callback) -> None:\n    \"\"\"Wire into the existing monitor; never starts a second worker.\"\"\"\n    global _HLS_RECOVERY_TICK\n    if not callable(callback):\n        raise ValueError('Recovery callback must be callable')\n    _HLS_RECOVERY_TICK = callback\n\n"

def checked(source, expected):
    if hashlib.sha256(source.encode()).hexdigest() != expected:
        raise ValueError('Reviewed source changed; refuse patch')
    ast.parse(source)

def patch_app(source):
    checked(source, APP_BEFORE)
    marker = "\nif __name__ == '__main__':\n"
    if source.count(marker) != 1 or source.count('    player.start_progress_monitor()') != 1:
        raise ValueError('App startup anchor changed')
    source = source.replace(marker, '\n'+APP_HELPER+marker, 1)
    source = source.replace('    player.start_progress_monitor()',
        '    player.configure_hls_recovery_tick(_hls_recovery_tick)\n    player.start_progress_monitor()', 1)
    ast.parse(source)
    return source

def patch_player(source):
    checked(source, PLAYER_BEFORE)
    old = """def _progress_monitor() -> None:
    while True:
        try:
            save_current_progress('periodic')
        except Exception:
            pass
        time.sleep(WATCH_INTERVAL_SECONDS)
"""
    new = """def _progress_monitor() -> None:
    while True:
        try:
            save_current_progress('periodic')
        except Exception:
            pass
        try:
            callback = _HLS_RECOVERY_TICK
            if callback is not None:
                callback()
        except Exception:
            pass
        time.sleep(WATCH_INTERVAL_SECONDS)
"""
    if source.count(old) != 1 or source.count('def switch_mode(mode: str)') != 1:
        raise ValueError('Player monitor/mode anchor changed')
    source = source.replace(old, PLAYER_HELPER+new, 1)
    source = source.replace('def switch_mode(mode: str)',
        '@_PLAYBACK_INTENT.operation\ndef switch_mode(mode: str)', 1)
    ast.parse(source)
    return source
