from __future__ import annotations

import importlib
import json
import os
import runpy
import subprocess
import sys

_TARGET = "skeleton_media.cast.player"

if __name__ == "__main__":
    runpy.run_module(_TARGET, run_name="__main__")
else:
    _impl = importlib.import_module(_TARGET)

    _original_play = _impl.play

    def _play_browser_resolved_url(job: dict, source: dict) -> dict:
        _impl.stop()
        process = subprocess.run(
            [
                _impl.BROWSER_MEDIA,
                'play',
                str(source.get('browser_profile') or job.get('browser_profile') or os.environ.get('SKELETON_MEDIA_BROWSER_PROFILE', 'default')),
                str(source.get('url') or job.get('page_url') or ''),
                str(int(source.get('browser_index') or 0)),
                str(job.get('job_id') or ''),
                str(source.get('source_id') or ''),
            ],
            text=True,
            capture_output=True,
            timeout=75,
            check=False,
        )
        if process.returncode:
            try:
                detail = str(json.loads(process.stdout or '{}').get('error') or '')
            except Exception:
                detail = ''
            raise RuntimeError(detail or (process.stderr or process.stdout or 'Chrome не відкрив відео.').strip()[-800:])
        data = json.loads(process.stdout or '{}')
        current = _impl.status()
        if not current.get('running') and not data.get('opened'):
            raise RuntimeError('Chrome прийняв команду, але сторінку на TV не підтверджено.')
        return {
            'accepted': True,
            'display_applied': True,
            'backend': 'chrome-browser',
            'browser': data,
            'player': current,
            'source': {
                'source_id': source.get('source_id'),
                'quality': source.get('quality'),
                'translation': source.get('translation'),
            },
        }

    def _play_with_browser_sources(job: dict, source: dict, subtitles: str = 'off') -> dict:
        if source.get('backend') == 'chrome-browser':
            return _impl.play_browser(job, source)
        return _original_play(job, source, subtitles)

    _impl.play_browser = _play_browser_resolved_url
    _impl.play = _play_with_browser_sources
    sys.modules[__name__] = _impl
