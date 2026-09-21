from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
import unicodedata

from .resolver import _cache_poster, _cache_youtube_landscape_poster, _cache_youtube_poster
from . import trakt_sync

HOME = Path(os.environ.get('SKELETON_MEDIA_HOME', str(Path.home()))).expanduser()
STATE = HOME / '.local/state/skeleton-cast'
LANGUAGE_PREFERENCES = STATE / 'language-preferences.json'
SOCKET = Path('/run/user/1000/skeleton-cast-mpv.sock')
PIDFILE = STATE / 'mpv.pid'
LOG = STATE / 'mpv.log'
LAUNCH = STATE / 'launch.json'
PLAYER_UNIT = 'skeleton-cast-player.service'
CURRENT = STATE / 'current.json'
IPTV_TRANSITION = STATE / 'tv-transition.json'
WATCH_HISTORY = STATE / 'watch-history.json'
WATCH_SCHEMA = 'skeleton.media.watch_history.v2'
LAST_VOD = STATE / 'last-vod.json'
LAST_VOD_SCHEMA = 'skeleton.media.last_vod.v1'
WATCH_INTERVAL_SECONDS = 5.0
WATCH_MIN_RESUME_SECONDS = 15.0
WATCH_COMPLETE_REMAINING_SECONDS = 20.0
WATCH_COMPLETE_RATIO = 0.995
_WATCH_LOCK = threading.RLock()
_PLAYBACK_LOCK = threading.RLock()
_PROGRESS_MONITOR_STARTED = False
JOBS = STATE / 'jobs'
POSTERS = STATE / 'posters'
MODE_FILE = HOME / '.local/state/tv-mode/current'
MODE_REQUEST_FILE = HOME / '.local/state/tv-mode/requested'
MODE_TARGET_FILE = STATE / 'mode-target.json'
MPV = '/usr/bin/mpv'
TV_MODE = str(HOME / '.local/bin/tv-mode')
CHROME_MEDIA = str(HOME / '.local/bin/home-edge-chrome-media')
BROWSER_MEDIA = str(HOME / '.local/bin/home-edge-browser-media')
DISPLAY_REFRESH = str(HOME / '.local/bin/home-edge-display-refresh')
XDOTOOL = '/usr/bin/xdotool'
SYSTEMCTL = '/usr/bin/systemctl'
FFPROBE = '/usr/bin/ffprobe'
MEDIA_MODE_UNIT = 'skeleton-cast-media-mode.service'
TV_MODE_UNIT_TEMPLATE = 'home-edge-tv-mode@{}.service'
UA = 'Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36'


def _atomic(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def _versioned_poster(value: Any) -> Any:
    raw = str(value or '').strip()
    if not raw.startswith('/posters/'):
        return value
    base, _, query = raw.partition('?')
    path = POSTERS / Path(base).name
    try:
        version = path.stat().st_mtime_ns
    except OSError:
        return value
    parts = [part for part in query.split('&') if part and not part.startswith('pv=')]
    parts.append(f'pv={version}')
    return base + '?' + '&'.join(parts)


def _catalog_metadata(job: dict[str, Any]) -> dict[str, Any]:
    return job.get('catalog') if isinstance(job.get('catalog'), dict) else {}


def _canonical_display_title(job: dict[str, Any], source: dict[str, Any] | None = None, fallback: Any = None) -> str:
    source = source if isinstance(source, dict) else {}
    catalog = _catalog_metadata(job)
    title = str(catalog.get('title') or job.get('title') or source.get('title') or fallback or source.get('page_title') or 'Home').strip()
    year = str(catalog.get('year') or '').strip()
    if year and re.fullmatch(r'(?:19|20)\d{2}', year) and not re.search(rf'\b{re.escape(year)}\b', title):
        return f'{title} ({year})'
    return title or 'Home'


def _canonical_season_poster(job: dict[str, Any], source: dict[str, Any] | None, season: Any, current: dict[str, Any] | None = None) -> Any:
    source = source if isinstance(source, dict) else {}
    current = current if isinstance(current, dict) else {}
    catalog = _catalog_metadata(job)
    season_key = str(season or '').strip()
    season_posters = catalog.get('season_posters') or job.get('season_posters') or {}
    mapped = season_posters.get(season_key) if isinstance(season_posters, dict) else None
    return (
        mapped
        or source.get('season_poster')
        or (source.get('poster') if source.get('poster_scope') == 'season' else None)
        or current.get('poster')
        or job.get('poster')
        or catalog.get('poster')
    )


def _current_metadata() -> dict[str, Any]:
    try:
        current = json.loads(CURRENT.read_text(encoding='utf-8'))
    except Exception:
        current = {}
    job: dict[str, Any] = {}
    job_id = current.get('job_id')
    if isinstance(job_id, str) and job_id:
        try:
            job = json.loads((JOBS / f'{job_id}.json').read_text(encoding='utf-8'))
        except Exception:
            job = {}
    source: dict[str, Any] = {}
    source_id = str(current.get('source_id') or '')
    if source_id:
        source = next((item for item in job.get('sources', []) if str(item.get('source_id') or '') == source_id), {})
    raw_title = str(current.get('title') or '').strip()
    display_title = _canonical_display_title(job, source, raw_title)
    season = str(source.get('season') or current.get('season') or job.get('season') or '')
    poster = _canonical_season_poster(job, source, season, current)
    return {
        **current,
        'display-title': display_title,
        'quality': current.get('quality'),
        'translation': current.get('translation'),
        'season': season or current.get('season'),
        'episode': current.get('episode'),
        'job_id': current.get('job_id'),
        'poster': _versioned_poster(poster),
        'poster_season': season or None,
    }



def _atomic_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _clean_media_title(value: str) -> tuple[str, str | None]:
    text = unicodedata.normalize('NFKC', str(value or '')).casefold().strip()
    year_match = re.search(r'\b((?:19|20)\d{2})\b', text)
    year = year_match.group(1) if year_match else None
    text = re.split(r'\s+[|/]\s+', text, maxsplit=1)[0]
    text = re.split(r'\b(?:онлайн|дивитися|смотреть|watch online)\b', text, maxsplit=1)[0]
    text = re.sub(r'^\s*(?:фільм|фильм|серіал|сериал|аніме|аниме|мультфільм|мультфильм|movie|series)\s+', '', text)
    text = re.sub(r'\b(?:19|20)\d{2}\b', ' ', text)
    text = re.sub(r'\b(?:сезон|season)\s*[-:#.]?\s*\d{1,3}\b|\b\d{1,3}\s*(?:сезон|season)\b', ' ', text)
    text = re.sub(r'\b(?:українською|украинском|російською|русском|мовою|языке|високій|высоком|якості|качестве|hd|fullhd|uhd|4k|1080p|720p|480p)\b', ' ', text)
    text = re.sub(r'[^0-9a-zа-яіїєґё]+', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text or 'unknown', year


def _episode_identity(value: Any) -> str | None:
    raw = unicodedata.normalize('NFKC', str(value or '')).casefold().strip()
    if not raw:
        return None
    season_episode = re.search(r'\bs\s*(\d{1,3})\s*e\s*(\d{1,4})\b', raw)
    if season_episode:
        return f's{int(season_episode.group(1))}e{int(season_episode.group(2))}'
    season = re.search(r'\b(?:сезон|season)\s*[-:#.]?\s*(\d{1,3})\b', raw)
    episode = re.search(r'\b(?:серія|серия|episode|ep)\s*[-:#.]?\s*(\d{1,4})\b', raw)
    if not episode:
        episode = re.search(r'\b(\d{1,4})\s*(?:серія|серия|episode)\b', raw)
    if episode:
        prefix = f's{int(season.group(1))}' if season else 's0'
        return f'{prefix}e{int(episode.group(1))}'
    return None


def _source_episode_identity(source: dict[str, Any]) -> str | None:
    season = str(source.get('season') or '').strip()
    episode = str(source.get('episode') or '').strip()
    if season and episode:
        return _episode_identity(f'season {season} {episode}')
    return _episode_identity(episode)


def content_identity(job: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    catalog = _catalog_metadata(job)
    raw_title = str(catalog.get('title') or job.get('title') or source.get('title') or source.get('page_title') or '')
    canonical_title, parsed_year = _clean_media_title(raw_title)
    catalog_year = str(catalog.get('year') or '').strip()
    year = catalog_year if re.fullmatch(r'(?:19|20)\d{2}', catalog_year) else parsed_year
    episode = _source_episode_identity(source)
    material = f'v3|{canonical_title}|{year or ""}|{episode or "film"}'
    key = hashlib.sha256(material.encode('utf-8')).hexdigest()
    return {
        'content_key': key,
        'canonical_title': canonical_title,
        'year': year,
        'episode_key': episode,
        'identity_scope': 'title_year_season_episode',
    }


def _load_watch_history() -> dict[str, Any]:
    with _WATCH_LOCK:
        try:
            data = json.loads(WATCH_HISTORY.read_text(encoding='utf-8'))
        except Exception:
            data = {}
        if not isinstance(data, dict) or data.get('schema') != WATCH_SCHEMA or not isinstance(data.get('items'), dict):
            data = {'schema': WATCH_SCHEMA, 'updated_at': int(time.time()), 'items': {}}
        return data


def _write_watch_history(data: dict[str, Any]) -> None:
    with _WATCH_LOCK:
        data['schema'] = WATCH_SCHEMA
        data['updated_at'] = int(time.time())
        _atomic_private(WATCH_HISTORY, data)


def _job_source_from_current(current: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    job_id = str(current.get('job_id') or '')
    source_id = str(current.get('source_id') or '')
    if not job_id or not source_id:
        return None
    try:
        job = json.loads((JOBS / f'{job_id}.json').read_text(encoding='utf-8'))
    except Exception:
        return None
    source = next((item for item in job.get('sources', []) if str(item.get('source_id') or '') == source_id), None)
    return (job, source) if isinstance(source, dict) else None


def _is_vod(job: dict[str, Any], source: dict[str, Any]) -> bool:
    return not bool(job.get('live') or source.get('live') or source.get('backend') == 'iptv')


def _resolve_vod_reference(job_id: str, source_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not job_id or not source_id:
        return None
    try:
        job = json.loads((JOBS / f'{job_id}.json').read_text(encoding='utf-8'))
    except Exception:
        return None
    source = next((item for item in job.get('sources', []) if str(item.get('source_id') or '') == source_id), None)
    if not isinstance(source, dict) or not _is_vod(job, source):
        return None
    return job, source


def _write_last_vod(job: dict[str, Any], source: dict[str, Any], position: float, duration: float, paused: bool, reason: str) -> dict[str, Any]:
    if not _is_vod(job, source):
        return {}
    identity = content_identity(job, source)
    record = {
        'schema': LAST_VOD_SCHEMA,
        'job_id': str(job.get('job_id') or ''),
        'source_id': str(source.get('source_id') or ''),
        'content_key': identity.get('content_key'),
        'display_title': _canonical_display_title(job, source),
        'position_seconds': round(max(0.0, float(position or 0.0)), 3),
        'duration_seconds': round(max(0.0, float(duration or source.get('duration') or 0.0)), 3),
        'paused': bool(paused),
        'reason': reason,
        'updated_at': int(time.time()),
    }
    _atomic_private(LAST_VOD, record)
    return record


def _last_vod_reference() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    try:
        record = json.loads(LAST_VOD.read_text(encoding='utf-8'))
    except Exception:
        record = {}
    if isinstance(record, dict) and record.get('schema') == LAST_VOD_SCHEMA:
        resolved = _resolve_vod_reference(str(record.get('job_id') or ''), str(record.get('source_id') or ''))
        if resolved is not None:
            return resolved[0], resolved[1], record

    history = _load_watch_history().get('items', {})
    candidates = sorted(
        (item for item in history.values() if isinstance(item, dict)),
        key=lambda item: int(item.get('updated_at') or 0),
        reverse=True,
    )
    for item in candidates:
        resolved = _resolve_vod_reference(str(item.get('last_job_id') or ''), str(item.get('last_source_id') or ''))
        if resolved is None:
            continue
        job, source = resolved
        record = _write_last_vod(
            job, source, float(item.get('position_seconds') or 0.0),
            float(item.get('duration_seconds') or source.get('duration') or 0.0),
            True, 'history_migration',
        )
        return job, source, record
    return None


def last_vod_status() -> dict[str, Any]:
    resolved = _last_vod_reference()
    if resolved is None:
        return {'available': False}
    job, source, record = resolved
    return {
        'available': True,
        'job_id': job.get('job_id'),
        'source_id': source.get('source_id'),
        'title': record.get('display_title') or job.get('title'),
        'position_seconds': float(record.get('position_seconds') or 0.0),
        'duration_seconds': float(record.get('duration_seconds') or source.get('duration') or 0.0),
        'paused': bool(record.get('paused')),
        'updated_at': record.get('updated_at'),
    }


def _completed(position: float, duration: float, eof_reached: bool = False) -> bool:
    if eof_reached:
        return True
    if duration <= 0:
        return False
    remaining = max(0.0, duration - position)
    return remaining <= WATCH_COMPLETE_REMAINING_SECONDS or position / duration >= WATCH_COMPLETE_RATIO


def _history_resume(job: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    identity = content_identity(job, source)
    if source.get('live') or source.get('backend') == 'iptv' or job.get('live'):
        return {**identity, 'resume_position': 0.0, 'previously_completed': False, 'history_found': False}
    item = _load_watch_history().get('items', {}).get(identity['content_key']) or {}
    completed = bool(item.get('completed'))
    position = float(item.get('position_seconds') or 0.0)
    duration = float(item.get('duration_seconds') or source.get('duration') or 0.0)
    if completed or position < WATCH_MIN_RESUME_SECONDS or _completed(position, duration):
        position = 0.0
    return {
        **identity,
        'resume_position': position,
        'previously_completed': completed,
        'history_found': bool(item),
    }


def _save_progress_snapshot(job: dict[str, Any], source: dict[str, Any], position: float, duration: float, eof_reached: bool, reason: str) -> dict[str, Any]:
    identity = content_identity(job, source)
    complete = _completed(position, duration, eof_reached)
    stored_position = 0.0 if complete or position < WATCH_MIN_RESUME_SECONDS else max(0.0, position)
    history = _load_watch_history()
    items = history.setdefault('items', {})
    previous = items.get(identity['content_key']) if isinstance(items.get(identity['content_key']), dict) else {}
    now = int(time.time())
    entry = {
        **identity,
        'display_title': _canonical_display_title(job, source),
        'position_seconds': round(stored_position, 3),
        'last_observed_position_seconds': round(max(0.0, position), 3),
        'duration_seconds': round(max(0.0, duration), 3),
        'completed': complete,
        'last_reason': reason,
        'last_job_id': job.get('job_id'),
        'last_source_id': source.get('source_id'),
        'last_site_host': job.get('site_host'),
        'last_translation': source.get('translation'),
        'updated_at': now,
        'first_seen_at': previous.get('first_seen_at') or now,
    }
    if complete:
        entry['completed_at'] = now
    elif previous.get('completed_at'):
        entry['completed_at'] = previous.get('completed_at')
    items[identity['content_key']] = entry
    _write_watch_history(history)
    return entry


def save_current_progress(reason: str = 'periodic') -> dict[str, Any] | None:
    with _PLAYBACK_LOCK:
        if not _player_active() or _current_tv_mode() != 'mpv':
            return None
        try:
            current = json.loads(CURRENT.read_text(encoding='utf-8'))
        except Exception:
            return None
        if current.get('live') or current.get('backend') == 'iptv':
            return None
        resolved = _job_source_from_current(current)
        if resolved is None:
            return None
        job, source = resolved
        try:
            position = command(['get_property', 'time-pos']).get('data')
            duration = command(['get_property', 'duration']).get('data')
            eof = command(['get_property', 'eof-reached']).get('data')
            paused = bool(command(['get_property', 'pause']).get('data'))
        except Exception:
            return None
        if not isinstance(position, (int, float)) or position < 0:
            return None
        if not isinstance(duration, (int, float)) or duration <= 0:
            duration = float(source.get('duration') or 0.0)
        entry = _save_progress_snapshot(job, source, float(position), float(duration), bool(eof), reason)
        _write_last_vod(job, source, float(entry.get('position_seconds') or position), float(entry.get('duration_seconds') or duration), paused, reason)
        try:
            trakt_sync.enqueue_progress(job, source, entry, paused, reason)
        except Exception:
            pass
        return entry


def _progress_monitor() -> None:
    while True:
        try:
            save_current_progress('periodic')
        except Exception:
            pass
        time.sleep(WATCH_INTERVAL_SECONDS)


def start_progress_monitor() -> bool:
    global _PROGRESS_MONITOR_STARTED
    with _WATCH_LOCK:
        if _PROGRESS_MONITOR_STARTED:
            return False
        _PROGRESS_MONITOR_STARTED = True
        threading.Thread(target=_progress_monitor, name='media-watch-progress', daemon=True).start()
        return True


def _apply_resume(position: float) -> float:
    if position < WATCH_MIN_RESUME_SECONDS:
        return 0.0
    result = command(['seek', float(position), 'absolute+exact'], timeout=5.0)
    if result.get('error') not in (None, 'success'):
        raise RuntimeError(f'MPV не застосував позицію відновлення: {result.get("error")}')
    last = 0.0
    for _ in range(40):
        actual = command(['get_property', 'time-pos']).get('data')
        if isinstance(actual, (int, float)):
            last = float(actual)
            if abs(last - position) <= 12.0:
                return last
        time.sleep(0.1)
    raise RuntimeError(f'Позицію відновлення не підтверджено: requested={position} actual={last}')


def suspend_current_vod(reason: str = 'before_mode_switch') -> dict[str, Any]:
    if not _player_active() or _current_tv_mode() != 'mpv':
        return {'suspended': False, 'reason': 'no_active_mpv'}
    try:
        current = json.loads(CURRENT.read_text(encoding='utf-8'))
    except Exception:
        current = {}
    if current.get('live') or current.get('backend') == 'iptv':
        return {'suspended': False, 'reason': 'live_stream'}
    resolved = _job_source_from_current(current)
    if resolved is None:
        return {'suspended': False, 'reason': 'unresolved_vod'}
    command(['set_property', 'pause', True], timeout=3.0)
    time.sleep(0.1)
    snapshot = save_current_progress(reason)
    # Keep the warm Wayland MPV surface mapped. On Debian 13 / MPV 0.40,
    # minimizing a warm fullscreen Wayland surface can leave it logically
    # unminimized but no longer presented after the next in-place restore.
    # GNOME Overview is handled separately, so parking only drops always-on-top.
    try:
        command(['set_property', 'window-minimized', False], timeout=3.0)
        command(['set_property', 'ontop', False], timeout=3.0)
    except Exception:
        pass
    return {'suspended': True, 'snapshot': snapshot, 'last_vod': last_vod_status()}


def restore_last_vod() -> dict[str, Any]:
    current_mode = mode_status(include_transition=False)
    if _player_active():
        try:
            current = json.loads(CURRENT.read_text(encoding='utf-8'))
        except Exception:
            current = {}
        resolved = _job_source_from_current(current)
        parkable = resolved is not None and _is_vod(resolved[0], resolved[1])
        if parkable:
            if _current_tv_mode() != 'mpv':
                _start_user_unit(TV_MODE_UNIT_TEMPLATE.format('mpv'))
            # Restore the parked surface as the sole fullscreen owner.
            command(['set_property', 'window-minimized', False], timeout=3.0)
            command(['set_property', 'fullscreen', True], timeout=3.0)
            command(['set_property', 'ontop', True], timeout=3.0)
            command(['set_property', 'pause', False], timeout=3.0)
            threading.Thread(target=_focus, daemon=True).start()
            return {**mode_status(), 'unchanged': True, 'restored': True, 'transition': 'vod-resume-in-place', 'last_vod': last_vod_status()}

    # Resolve the VOD before touching the live MPV session. TV and Cast share
    # the same MPV process, so TV→Cast must replace the live stream in-place.
    # Stopping TV first exposed the preserved warm YouTube window behind MPV.
    resolved = _last_vod_reference()
    if resolved is None:
        if current_mode.get('mode') == 'tv':
            stop()
        if _current_tv_mode() != 'mpv':
            _start_user_unit(TV_MODE_UNIT_TEMPLATE.format('mpv'))
        _switch_to_media()
        return {**mode_status(), 'unchanged': False, 'restored': False, 'transition': 'video-mode-empty', 'last_vod': {'available': False}}

    job, source, record = resolved
    tv_in_place = current_mode.get('mode') == 'tv' and _player_active() and _current_tv_mode() == 'mpv'
    if not tv_in_place and _current_tv_mode() != 'mpv':
        _start_user_unit(TV_MODE_UNIT_TEMPLATE.format('mpv'))
    result = play(job, source, 'off')
    command(['set_property', 'pause', False], timeout=3.0)
    transition = 'tv-to-vod-in-place' if tv_in_place and result.get('session_reused') else 'last-vod-restored'
    return {**mode_status(), 'unchanged': False, 'restored': True, 'transition': transition, 'last_vod': last_vod_status(), 'playback': result}


def command(parts: list[Any], timeout: float = 2.0) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(SOCKET))
        client.sendall((json.dumps({'command': parts}) + '\n').encode())
        data = b''
        while b'\n' not in data:
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.split(b'\n', 1)[0] or b'{}')
    finally:
        client.close()


def _unit_player_pid() -> int:
    r = subprocess.run([SYSTEMCTL, '--user', 'show', PLAYER_UNIT, '-p', 'MainPID', '--value'], text=True, capture_output=True, timeout=5, check=False)
    try:
        return int(r.stdout.strip() or 0)
    except ValueError:
        return 0


def _pidfile_pid() -> int:
    try:
        return int(PIDFILE.read_text(encoding='ascii').strip())
    except Exception:
        return 0


def _player_pid() -> int:
    for pid in (_unit_player_pid(), _pidfile_pid()):
        if pid > 0 and Path(f'/proc/{pid}').exists():
            return pid
    return 0


def _player_active() -> bool:
    return _player_pid() > 0 and SOCKET.exists()


def stop() -> None:
    with _PLAYBACK_LOCK:
        try:
            save_current_progress('stop')
        except Exception:
            pass
        try:
            command(['quit'])
        except Exception:
            pass
        subprocess.run([SYSTEMCTL, '--user', 'stop', PLAYER_UNIT], text=True, capture_output=True, timeout=15, check=False)
        for _ in range(30):
            if not SOCKET.exists():
                break
            time.sleep(0.1)
        SOCKET.unlink(missing_ok=True)
        PIDFILE.unlink(missing_ok=True)
        LAUNCH.unlink(missing_ok=True)


def _active_tty() -> str:
    try:
        return Path('/sys/class/tty/tty0/active').read_text(encoding='ascii').strip()
    except Exception:
        return 'unknown'


def _current_tv_mode() -> str:
    try:
        return MODE_FILE.read_text(encoding='utf-8').strip()
    except Exception:
        return 'unknown'


def set_mode_transition_target(target: str | None) -> None:
    if target is None:
        MODE_TARGET_FILE.unlink(missing_ok=True)
        return
    value = str(target).strip().lower()
    if value not in {'tv', 'mpv', 'kiosk', 'chrome', 'games', 'off'}:
        raise ValueError('Невідомий цільовий відеорежим.')
    MODE_TARGET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic(MODE_TARGET_FILE, {
        'schema': 'skeleton.media.mode_transition.v1',
        'target': value,
        'started_at': time.time(),
    })


def _mode_transition_target() -> str | None:
    try:
        data = json.loads(MODE_TARGET_FILE.read_text(encoding='utf-8'))
        started = float(data.get('started_at') or 0.0)
        target = str(data.get('target') or '').strip().lower()
        if data.get('schema') != 'skeleton.media.mode_transition.v1':
            return None
        if target not in {'tv', 'mpv', 'kiosk', 'chrome', 'games', 'off'}:
            return None
        if not (0.0 <= time.time() - started <= 180.0):
            return None
        return target
    except Exception:
        return None


def _start_user_unit(unit: str) -> None:
    result = subprocess.run([SYSTEMCTL, '--user', 'start', '--wait', unit], text=True, capture_output=True, timeout=85, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or f'Не вдалося запустити {unit}.').strip()[-600:])


def _switch_to_media() -> None:
    _start_user_unit(MEDIA_MODE_UNIT)
    for _ in range(30):
        if _active_tty() == 'tty2' and Path('/run/user/1000/wayland-0').exists():
            return
        time.sleep(0.2)
    raise RuntimeError(f'Медіарежим не застосовано: active_tty={_active_tty()}')



def _wayland() -> str:
    return 'wayland-0'


def _sway_socket() -> str | None:
    sockets = sorted(Path('/run/user/1000').glob('sway-ipc.1000.*.sock'), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(sockets[0]) if sockets else None


def _exit_gnome_overview() -> None:
    env = os.environ.copy()
    env.update({
        'XDG_RUNTIME_DIR': '/run/user/1000',
        'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus',
    })
    subprocess.run([
        '/usr/bin/gdbus', 'call', '--session', '--dest', 'org.gnome.Shell',
        '--object-path', '/org/gnome/Shell', '--method',
        'org.freedesktop.DBus.Properties.Set', 'org.gnome.Shell',
        'OverviewActive', '<false>'
    ], env=env, text=True, capture_output=True, timeout=3, check=False)


def _focus() -> None:
    _exit_gnome_overview()
    time.sleep(1.2)
    sock = _sway_socket()
    if not sock or not Path('/usr/bin/swaymsg').exists():
        return
    env = os.environ.copy()
    env.update({'XDG_RUNTIME_DIR': '/run/user/1000', 'SWAYSOCK': sock})
    for rule in ('[app_id="mpv"] focus', '[app_id="mpv"] fullscreen enable', '[title="Skeleton Cast"] focus'):
        subprocess.run(['/usr/bin/swaymsg', '-s', sock, rule], env=env, capture_output=True, timeout=3, check=False)



def _same_media_episode(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _source_episode_identity(left) == _source_episode_identity(right)


def _probe_source_audio(source: dict[str, Any], timeout: float = 18.0) -> bool | None:
    explicit = source.get('has_audio')
    if explicit is True or explicit is False:
        return explicit
    url = source.get('url')
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        return None
    if source.get('kind') == 'youtube' and source.get('ytdl_format'):
        return True
    headers = dict(source.get('headers') or {})
    cmd = [
        FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_type,codec_name',
        '-of', 'json', '-read_intervals', '%+4',
    ]
    user_agent = headers.get('User-Agent')
    referer = headers.get('Referer')
    extra = []
    for key in ('Origin', 'Cookie', 'Authorization'):
        if headers.get(key):
            extra.append(f'{key}: {headers[key]}')
    if user_agent:
        cmd.extend(['-user_agent', str(user_agent)])
    header_lines = []
    if referer:
        header_lines.append(f'Referer: {referer}')
    header_lines.extend(extra)
    if header_lines:
        cmd.extend(['-headers', '\r\n'.join(header_lines) + '\r\n'])
    cmd.append(url)
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
        if result.returncode:
            return None
        data = json.loads(result.stdout or '{}')
    except Exception:
        return None
    streams = data.get('streams') if isinstance(data, dict) else None
    if not isinstance(streams, list):
        return None
    return any(isinstance(stream, dict) and stream.get('codec_type') == 'audio' for stream in streams)



def _preferred_content_language() -> str:
    try:
        data=json.loads(LANGUAGE_PREFERENCES.read_text(encoding='utf-8'))
        value=str(data.get('content_language') or 'uk').lower().split('-',1)[0]
    except Exception:
        value='uk'
    return value if value in {'uk','ru','en','de'} else 'uk'

def _source_audio_language(source: dict[str, Any]) -> str:
    explicit = str(source.get('audio_language') or '').strip().lower()
    if explicit in {'uk','ukr','ua'}:
        return 'uk'
    if explicit in {'ru','rus'}:
        return 'ru'
    if explicit in {'en','eng'}:
        return 'en'
    if explicit in {'de','deu','ger'}:
        return 'de'
    text = ' '.join(str(source.get(key) or '') for key in ('group','translation','title','page_title','discovery_page_title')).casefold()
    ru = ('русский','русская','русское','рус.','російською','russian','lostfilm','newstudio','кубик в кубе','hdrezka studio','red head sound')
    uk = ('українською','українська','український','ukrainian','ukr','dniprofilm','le-doyen','так треба','цікава ідея','postmodern','постмодерн','1+1','плюс плюс','новий канал','ictv','мегого','megogo','sweet.tv','kyivstar')
    if any(x in text for x in ru) or any(ch in text for ch in ('ы','э','ъ','ё')):
        return 'ru'
    if any(x in text for x in uk) or any(ch in text for ch in ('і','ї','є','ґ')):
        return 'uk'
    return 'unknown'

def _audio_candidate_rank(source: dict[str, Any]) -> tuple[int, int, int, float]:
    lang=_source_audio_language(source); pref=_preferred_content_language(); order=[pref]+[x for x in ('uk','ru','en','de','unknown') if x!=pref]; language_rank=order.index(lang) if lang in order else len(order)
    return (
        0 if source.get('has_audio') is True else 1,
        language_rank,
        -int(source.get('height') or 0),
        0 if str(source.get('quality') or '').startswith('Авто') else 1,
        -float(source.get('tbr') or 0),
    )


def _prefer_audio_source(job: dict[str, Any], requested: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    requested_audio = _probe_source_audio(requested)
    if requested_audio is True:
        requested['has_audio'] = True
        return requested, False
    if requested_audio is False:
        requested['has_audio'] = False

    candidates = [
        item for item in job.get('sources', [])
        if isinstance(item, dict)
        and str(item.get('source_id') or '') != str(requested.get('source_id') or '')
        and _same_media_episode(item, requested)
        and item.get('has_drm') is not True
    ]
    candidates.sort(key=_audio_candidate_rank)
    for candidate in candidates:
        state = _probe_source_audio(candidate)
        if state is True:
            candidate['has_audio'] = True
            return candidate, True
        if state is False:
            candidate['has_audio'] = False
    if requested_audio is None:
        raise RuntimeError('Не вдалося підтвердити аудіодоріжку цього потоку і не знайдено перевіреного звукового варіанта.')
    raise RuntimeError('Обраний потік не має аудіодоріжки, а звукового варіанта для цього відео не знайдено.')


def _verify_loaded_audio(timeout: float = 8.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last_tracks: list[dict[str, Any]] = []
    while time.time() < deadline:
        tracks = command(['get_property', 'track-list']).get('data') or []
        last_tracks = [track for track in tracks if isinstance(track, dict)]
        selected = [
            track for track in last_tracks
            if track.get('type') == 'audio' and track.get('selected')
        ]
        if selected:
            codec = command(['get_property', 'audio-codec-name']).get('data')
            params = command(['get_property', 'audio-params']).get('data')
            if codec and isinstance(params, dict):
                return {'audio_track_verified': True, 'audio_codec': codec, 'audio_params': params}
        time.sleep(0.2)
    summary = [
        {'type': track.get('type'), 'codec': track.get('codec'), 'selected': track.get('selected')}
        for track in last_tracks
    ]
    raise RuntimeError(f'Завантажений потік не підтвердив активну аудіодоріжку: {summary}')


def _mpv_source_configuration(job: dict[str, Any], source: dict[str, Any], subtitles: str) -> tuple[str, list[str], dict[str, str]]:
    url = source.get('url')
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        raise RuntimeError('Некоректний URL потоку.')

    headers = dict(source.get('headers') or {})
    user_agent = str(headers.get('User-Agent') or UA)
    is_iptv = source.get('kind') == 'iptv' or source.get('backend') == 'iptv'
    referer = str(headers.get('Referer') or ('' if is_iptv else job.get('page_url') or ''))
    extra_headers: list[str] = []
    for key in ('Origin', 'Cookie', 'Authorization'):
        value = headers.get(key)
        if value:
            extra_headers.append(f'{key}: {value}')

    title = str(source.get('page_title') or job.get('title') or source.get('title') or 'Skeleton Cast')
    options: dict[str, str] = {
        'user-agent': user_agent,
        'referrer': referer,
        'http-header-fields': ','.join(extra_headers),
        'alang': 'ukr,uk,rus,ru,en,eng',
        'audio-display': 'no',
        'keep-open': 'no' if is_iptv else 'yes',
        'force-media-title': title,
    }
    hls_bitrate = source.get('hls_bitrate')
    if isinstance(hls_bitrate, (int, float)) and hls_bitrate > 0:
        options['hls-bitrate'] = str(int(hls_bitrate))
    ytdl_format = source.get('ytdl_format')
    if source.get('kind') == 'youtube':
        options['ytdl'] = 'yes'
        if ytdl_format:
            options['ytdl-format'] = str(ytdl_format)
    else:
        options['ytdl'] = 'no'
        options['ytdl-format'] = ''
    if subtitles == 'off':
        options['sid'] = 'no'
    else:
        options['sid'] = 'auto'
        options['slang'] = f'{subtitles},ukr,uk,en'
    return url, extra_headers, options


def _mpv_launch_args(job: dict[str, Any], source: dict[str, Any], subtitles: str, url: str, extra_headers: list[str], options: dict[str, str]) -> list[str]:
    is_iptv = source.get('kind') == 'iptv' or source.get('backend') == 'iptv'
    args = [
        MPV, '--fullscreen=yes', '--force-window=yes', '--keep-open=' + ('no' if is_iptv else 'yes'), '--no-terminal',
        '--gpu-context=wayland', '--no-border', '--ontop', '--screen=0', '--fs-screen=0', '--geometry=100%:100%',
        '--msg-level=all=warn', f'--input-ipc-server={SOCKET}', '--input-default-bindings=yes',
        '--osc=no', '--osd-level=0', '--cursor-autohide=always', '--hwdec=auto-safe', '--audio-client-name=SkeletonCast',
        '--title=Skeleton Cast', f'--user-agent={options["user-agent"]}',
        '--alang=ukr,uk,rus,ru,en,eng', '--audio-display=no',
    ]
    if options.get('referrer'):
        args.append(f'--referrer={options["referrer"]}')
    if options.get('hls-bitrate'):
        args.append(f'--hls-bitrate={options["hls-bitrate"]}')
    if is_iptv:
        # Live TV starts close to the live edge: one short initial buffer,
        # then no cache-induced pause that could accumulate a long delay.
        args.extend(['--cache=yes','--cache-pause=no','--cache-pause-initial=yes','--cache-pause-wait=1','--demuxer-readahead-secs=3','--stream-buffer-size=1MiB','--network-timeout=10'])
    elif options.get('hls-bitrate') or '.m3u8' in url.lower():
        # Resolvers frequently return a direct HLS variant rather than a master
        # playlist with hls_bitrate metadata. Direct variants still need the
        # normal VOD read-ahead policy or provider latency becomes visible as
        # playback stalls (notably Mars Express on hdvbua).
        args.extend(['--cache=yes','--cache-pause=yes','--cache-pause-initial=yes','--cache-pause-wait=6','--demuxer-readahead-secs=30','--stream-buffer-size=4MiB','--network-timeout=15'])
    if source.get('kind') == 'youtube':
        args.append('--ytdl=yes')
        if options.get('ytdl-format'):
            args.append(f'--ytdl-format={options["ytdl-format"]}')
    if subtitles == 'off':
        args.append('--sid=no')
    elif subtitles:
        args.extend(['--sid=auto', f'--slang={subtitles},ukr,uk,en'])
    if extra_headers:
        args.append('--http-header-fields=' + ','.join(extra_headers))
    args.append(url)
    return args


def _current_record(job: dict[str, Any], source: dict[str, Any], pid: int, resume: dict[str, Any] | None = None) -> dict[str, Any]:
    identity = content_identity(job, source)
    catalog = _catalog_metadata(job)
    is_movie = str(catalog.get('media_type') or job.get('history_media_type') or '').lower() == 'movie'
    season = '' if is_movie else str(source.get('season') or job.get('season') or '')
    episode = None if is_movie else source.get('episode')
    return {
        'pid': pid, 'job_id': job.get('job_id'), 'source_id': source.get('source_id'),
        'title': _canonical_display_title(job, source),
        'quality': source.get('quality'), 'translation': source.get('translation'),
        'season': season, 'episode': episode, 'poster': _canonical_season_poster(job, source, season), 'started_at': int(time.time()),
        'backend': source.get('backend'), 'live': bool(source.get('live')),
        'channel_id': source.get('channel_id'), 'channel_number': source.get('channel_number'),
        **identity,
        'resume_from_seconds': round(float((resume or {}).get('resume_position') or 0.0), 3),
    }


def _wait_for_loaded_file(pid: int, url: str, attempts: int) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for _ in range(attempts):
        live_pid = _player_pid()
        if live_pid != pid or live_pid <= 0:
            raise RuntimeError(f'MPV-сесія змінила PID під час заміни контенту: before={pid} after={live_pid}.')
        if SOCKET.exists():
            try:
                path = command(['get_property', 'path']).get('data')
                stream_path = command(['get_property', 'stream-open-filename']).get('data')
                vo = command(['get_property', 'vo-configured']).get('data')
                pos = command(['get_property', 'time-pos']).get('data')
                paused = command(['get_property', 'pause']).get('data')
                fullscreen = command(['get_property', 'fullscreen']).get('data')
                maximized = command(['get_property', 'window-maximized']).get('data')
                osd_w = command(['get_property', 'osd-width']).get('data')
                osd_h = command(['get_property', 'osd-height']).get('data')
                last = {
                    'path': path, 'stream_path': stream_path, 'vo_configured': vo,
                    'time_pos': pos, 'pause': paused, 'fullscreen': fullscreen,
                    'window_maximized': maximized, 'osd_width': osd_w,
                    'osd_height': osd_h, 'active_tty': _active_tty(),
                }
                loaded = path == url or stream_path == url
                if loaded and vo is True and isinstance(pos, (int, float)) and _active_tty() == 'tty2':
                    return last
            except Exception as exc:
                last = {'error': f'{type(exc).__name__}: {exc}'}
        time.sleep(0.2)
    raise RuntimeError(f'Новий потік не підтвердив відтворення у чинній MPV-сесії: {last}')



def _set_display_refresh(hz: int) -> dict[str, Any]:
    if hz not in {50, 60}:
        raise ValueError('Unsupported display refresh policy.')
    process = subprocess.run([DISPLAY_REFRESH, str(hz)], text=True, capture_output=True, timeout=7, check=False)
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout or f'Display refresh {hz} Hz failed.').strip()[-700:])
    try:
        return json.loads(process.stdout or '{}')
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'Invalid display refresh response for {hz} Hz.') from exc


def _apply_iptv_buffer_policy() -> None:
    _set_display_refresh(50)
    for name, value in (
        ('demuxer-readahead-secs', 8),
        ('cache-pause-wait', 2),
        ('cache-pause', False),
        ('cache-pause-initial', True),
        ('stream-buffer-size', 4194304),
    ):
        result = command(['set_property', name, value], timeout=3.0)
        if result.get('error') not in (None, 'success'):
            raise RuntimeError(f'MPV не застосував IPTV-буфер {name}: {result.get("error")}')


def _apply_vod_buffer_policy() -> None:
    _set_display_refresh(60)
    # IPTV deliberately uses a tiny live-edge buffer. Reset those mutable MPV
    # properties before an in-place TV→VOD load so Cast regains normal VOD buffering.
    for name, value in (
        ('demuxer-readahead-secs', 30),
        ('cache-pause-wait', 6),
        ('cache-pause', True),
        ('cache-pause-initial', True),
        ('stream-buffer-size', 4194304),
    ):
        result = command(['set_property', name, value], timeout=3.0)
        if result.get('error') not in (None, 'success'):
            raise RuntimeError(f'MPV не застосував VOD-буфер {name}: {result.get("error")}')


def _replace_active_mpv(job: dict[str, Any], source: dict[str, Any], subtitles: str, url: str, options: dict[str, str], launch_args: list[str], resume: dict[str, Any]) -> dict[str, Any]:
    pid = _player_pid()
    if pid <= 0 or not _player_active() or _current_tv_mode() != 'mpv' or _active_tty() != 'tty2':
        raise RuntimeError('MPV-сесія не придатна для in-place заміни.')
    health = command(['get_property', 'vo-configured'])
    if health.get('error') not in (None, 'success') or health.get('data') is not True:
        raise RuntimeError('Активна MPV-сесія не підтвердила готовий відеовихід.')
    if source.get('kind') == 'iptv' or source.get('backend') == 'iptv':
        _apply_iptv_buffer_policy()
    else:
        _apply_vod_buffer_policy()

    result = command(['loadfile', url, 'replace', -1, options], timeout=5.0)
    if result.get('error') not in (None, 'success'):
        raise RuntimeError(f'MPV не прийняв in-place заміну: {result.get("error")}')
    command(['set_property', 'pause', False])
    if source.get('kind') == 'iptv' or source.get('backend') == 'iptv':
        command(['set_property', 'speed', 1.0])
    # A warm VOD is deliberately minimized while Chrome/YouTube owns the TV.
    # Any in-place MPV content replacement (VOD or IPTV) becomes the foreground
    # owner again, so explicitly remap the Wayland surface before fullscreen.
    command(['set_property', 'window-minimized', False])
    command(['set_property', 'ontop', True])
    command(['set_property', 'fullscreen', True])
    command(['set_property', 'window-maximized', True])

    attempts = 750 if source.get('kind') == 'youtube' else 150
    verified = _wait_for_loaded_file(pid, url, attempts)
    resumed_at = _apply_resume(float(resume.get('resume_position') or 0.0))
    audio_verified = _verify_loaded_audio()
    _atomic(LAUNCH, {'args': launch_args})
    _atomic(CURRENT, _current_record(job, source, pid, resume))
    if _is_vod(job, source):
        _write_last_vod(job, source, float(resumed_at), float(source.get('duration') or 0.0), False, 'play')
    threading.Thread(target=_focus, daemon=True).start()
    return {
        'pid': pid, 'pid_before': pid, 'pid_after': _player_pid(),
        'accepted': True, 'display_applied': True,
        'session_reused': True, 'transition': 'in-place-loadfile',
        'content_key': resume.get('content_key'),
        'history_found': bool(resume.get('history_found')),
        'previously_completed': bool(resume.get('previously_completed')),
        'resume_requested_seconds': float(resume.get('resume_position') or 0.0),
        'resume_applied_seconds': resumed_at,
        'source': {
            'source_id': source.get('source_id'),
            'quality': source.get('quality'),
            'translation': source.get('translation'),
            'has_audio': True,
        },
        **audio_verified,
        **verified,
    }


def play(job: dict[str, Any], source: dict[str, Any], subtitles: str = 'off') -> dict[str, Any]:
    if source.get('has_drm'):
        raise RuntimeError('Потік має DRM і не підтримується локальним MPV.')

    with _PLAYBACK_LOCK:
        try:
            save_current_progress('before_switch')
        except Exception:
            pass
        requested_source = source
        source, audio_fallback_used = _prefer_audio_source(job, requested_source)
        resume = _history_resume(job, source)
        url, extra_headers, options = _mpv_source_configuration(job, source, subtitles)
        args = _mpv_launch_args(job, source, subtitles, url, extra_headers, options)

        if _current_tv_mode() == 'mpv' and _active_tty() == 'tty2' and _player_active():
            active_pid = _player_pid()
            try:
                result = _replace_active_mpv(job, source, subtitles, url, options, args, resume)
                result['audio_fallback_used'] = audio_fallback_used
                result['requested_source_id'] = requested_source.get('source_id')
                return result
            except Exception as exc:
                # Debian 13 ships MPV 0.40. If an in-place transition is rejected
                # or the new stream cannot be verified, continue through the
                # canonical cold-start path instead of leaving the previous
                # title playing while the UI reports a new selection.
                in_place_error = f'{type(exc).__name__}: {exc}'
            else:
                in_place_error = ''
        else:
            in_place_error = ''

        stop()
        # Enter MPV through the canonical controller so warm YouTube is paused,
        # tv-mode/current becomes mpv, and subsequent status/control routing is correct.
        _start_user_unit(TV_MODE_UNIT_TEMPLATE.format('mpv'))
        STATE.mkdir(parents=True, exist_ok=True)
        _atomic(LAUNCH, {'args': args})
        subprocess.run([SYSTEMCTL, '--user', 'reset-failed', PLAYER_UNIT], text=True, capture_output=True, timeout=10, check=False)
        started = subprocess.run([SYSTEMCTL, '--user', 'start', PLAYER_UNIT], text=True, capture_output=True, timeout=20, check=False)
        if started.returncode:
            raise RuntimeError((started.stderr or started.stdout or 'Не вдалося запустити окремий MPV-сервіс.').strip()[-700:])
        pid = 0
        for _ in range(40):
            pid = _unit_player_pid()
            if pid > 0 and Path(f'/proc/{pid}').exists():
                break
            time.sleep(0.1)
        if pid <= 0:
            code = subprocess.run([SYSTEMCTL, '--user', 'show', PLAYER_UNIT, '-p', 'ExecMainStatus', '--value'], text=True, capture_output=True, timeout=5, check=False).stdout.strip()
            raise RuntimeError(f"Окремий MPV-сервіс не запустився, код {code or 'невідомо'}.")
        PIDFILE.write_text(str(pid) + '\n', encoding='ascii')
        threading.Thread(target=_focus, daemon=True).start()

        attempts = 750 if source.get('kind') == 'youtube' else 150
        try:
            last = _wait_for_loaded_file(pid, url, attempts)
            resumed_at = _apply_resume(float(resume.get('resume_position') or 0.0))
            audio_verified = _verify_loaded_audio()
            _atomic(CURRENT, _current_record(job, source, pid, resume))
            if _is_vod(job, source):
                _write_last_vod(job, source, float(resumed_at), float(source.get('duration') or 0.0), False, 'play')
            return {
                'pid': pid, 'accepted': True, 'display_applied': True,
                'session_reused': False,
                'transition': 'cold-start-after-in-place-failure' if in_place_error else 'cold-start',
                'in_place_fallback': bool(in_place_error),
                'content_key': resume.get('content_key'),
                'history_found': bool(resume.get('history_found')),
                'previously_completed': bool(resume.get('previously_completed')),
                'resume_requested_seconds': float(resume.get('resume_position') or 0.0),
                'resume_applied_seconds': resumed_at,
                'audio_fallback_used': audio_fallback_used,
                'requested_source_id': requested_source.get('source_id'),
                'source': {
                    'source_id': source.get('source_id'),
                    'quality': source.get('quality'),
                    'translation': source.get('translation'),
                    'has_audio': True,
                },
                **audio_verified,
                **last,
            }
        except Exception as exc:
            # Fail closed in Cast/MPV mode. A failed VOD source must never
            # resurrect or foreground an unrelated warm YouTube session.
            stop()
            raise RuntimeError(f'Плеєр не з’явився на TV або потік не почався: {exc}') from exc


def play_browser(job: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    stop()
    process = subprocess.run(
        [BROWSER_MEDIA, 'play', str(source.get('browser_profile') or job.get('browser_profile') or os.environ.get('SKELETON_MEDIA_BROWSER_PROFILE', 'default')),
         str(source.get('url') or job.get('page_url') or ''), str(int(source.get('browser_index') or 0)),
         str(job.get('job_id') or ''), str(source.get('source_id') or '')],
        text=True, capture_output=True, timeout=75, check=False,
    )
    if process.returncode:
        try:
            detail = str(json.loads(process.stdout or '{}').get('error') or '')
        except Exception:
            detail = ''
        raise RuntimeError(detail or (process.stderr or process.stdout or 'Chrome не відкрив відео.').strip()[-800:])
    data = json.loads(process.stdout or '{}')
    current = status()
    if not current.get('running') and not data.get('opened'):
        raise RuntimeError('Chrome прийняв команду, але сторінку на TV не підтверджено.')
    return {
        'accepted': True, 'display_applied': True, 'backend': 'chrome-browser',
        'browser': data, 'player': current,
        'source': {'source_id': source.get('source_id'), 'quality': source.get('quality'), 'translation': source.get('translation')},
    }


def _browser_media_status(force: bool = False) -> dict[str, Any] | None:
    if not force and _current_tv_mode() != 'chrome':
        return None
    try:
        probe = subprocess.run([BROWSER_MEDIA, 'status'], text=True, capture_output=True, timeout=5, check=False)
        data = json.loads(probe.stdout or '{}')
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault('backend', 'chrome-browser')
    data.setdefault('source', 'Chrome')
    return data


def _youtube_web_media_status(force: bool = False) -> dict[str, Any] | None:
    if not force and _current_tv_mode() != 'kiosk':
        return None
    try:
        probe = subprocess.run([CHROME_MEDIA, 'status'], text=True, capture_output=True, timeout=3, check=False)
        data = json.loads(probe.stdout or '{}')
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault('backend', 'youtube-web')
    data.setdefault('source', 'YouTube Web')
    poster = str(data.get('poster') or '')
    if poster.startswith(('http://', 'https://')):
        try:
            referer = str(data.get('url') or 'https://www.youtube.com/')
            landscape = _cache_youtube_landscape_poster(poster, referer)
            cached = _cache_youtube_poster(poster, referer)
            if landscape:
                data['poster_landscape'] = landscape
            if cached:
                data['poster'] = cached
        except Exception:
            pass
    return data


def _youtube_page_ready(value: dict[str, Any] | None) -> bool:
    data = value if isinstance(value, dict) else {}
    url = str(data.get('url') or '')
    return url.startswith(('https://www.youtube.com/tv', 'http://www.youtube.com/tv')) and not data.get('error')


def _active_backend() -> tuple[str, dict[str, Any] | None]:
    # The explicit tv-mode selection is authoritative. Persistent Chrome and
    # MPV processes are intentionally kept warm in the background, therefore
    # process/media liveness alone must never cancel a user-selected mode.
    raw = _current_tv_mode()
    if raw == 'mpv':
        return 'mpv', None
    if raw in {'games', 'off'}:
        return raw, None
    if raw == 'kiosk':
        web = _youtube_web_media_status(force=True)
        return 'kiosk', web if isinstance(web, dict) else None
    if raw == 'chrome':
        browser = _browser_media_status(force=True)
        return 'chrome', browser if isinstance(browser, dict) else None

    # Recovery-only fallback for missing/unknown canonical state.
    web = _youtube_web_media_status(force=True)
    if isinstance(web, dict) and web.get('running'):
        return 'kiosk', web
    browser = _browser_media_status(force=True)
    if isinstance(browser, dict) and browser.get('running'):
        return 'chrome', browser
    if _player_active():
        return 'mpv', None
    if _youtube_page_ready(web):
        return 'kiosk', web
    return raw, None


def status() -> dict[str, Any]:
    backend, detected = _active_backend()
    if detected is not None:
        return detected
    if backend == 'kiosk':
        web = _youtube_web_media_status(force=True)
        if web is not None:
            return web
    if backend == 'chrome':
        browser = _browser_media_status(force=True)
        if browser is not None:
            return browser
    if not _player_active():
        return {'running': False}
    metadata = _current_metadata()
    out: dict[str, Any] = {
        'running': True,
        'display-title': metadata.get('display-title'),
        'quality': metadata.get('quality'),
        'translation': metadata.get('translation'),
        'season': metadata.get('season'),
        'episode': metadata.get('episode'),
        'job_id': metadata.get('job_id'),
        'source_id': metadata.get('source_id'),
        'poster': metadata.get('poster'),
        'poster_season': metadata.get('poster_season'),
        'backend': metadata.get('backend'),
        'live': metadata.get('live'),
        'channel_id': metadata.get('channel_id'),
        'channel_number': metadata.get('channel_number'),
    }
    for prop in ('pause', 'time-pos', 'duration', 'media-title', 'aid', 'sid', 'fullscreen', 'window-maximized', 'osd-width', 'osd-height'):
        try:
            out[prop] = command(['get_property', prop]).get('data')
        except Exception:
            out[prop] = None
    try:
        tracks = command(['get_property', 'track-list']).get('data') or []
    except Exception:
        tracks = []
    def public_track(track: dict[str, Any]) -> dict[str, Any]:
        return {
            key: track.get(key)
            for key in ('id', 'type', 'title', 'lang', 'codec', 'selected', 'default', 'forced',
                        'external', 'audio-channels', 'demux-channels', 'demux-samplerate')
            if track.get(key) is not None
        }
    out['audio_tracks'] = [public_track(track) for track in tracks if isinstance(track, dict) and track.get('type') == 'audio']
    out['subtitle_tracks'] = [public_track(track) for track in tracks if isinstance(track, dict) and track.get('type') == 'sub']
    return out


def select_track(kind: str, track_id: Any) -> dict[str, Any]:
    active_backend, _detected = _active_backend()
    if active_backend in {'chrome', 'kiosk'}:
        raise RuntimeError('Браузерний режим керує доріжками на самій сторінці.')
    if not _player_active():
        raise RuntimeError('MPV не запущений.')
    if kind not in {'audio', 'subtitle'}:
        raise ValueError('Невідомий тип доріжки.')
    track_type = 'audio' if kind == 'audio' else 'sub'
    prop = 'aid' if kind == 'audio' else 'sid'
    tracks = command(['get_property', 'track-list']).get('data') or []
    available = {
        int(track.get('id'))
        for track in tracks
        if isinstance(track, dict) and track.get('type') == track_type and isinstance(track.get('id'), int)
    }
    raw = str(track_id).strip().lower()
    if kind == 'subtitle' and raw in {'off', 'no', 'false', '0'}:
        value: Any = 'no'
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError('Некоректна доріжка.')
        if value not in available:
            raise ValueError('Доріжка більше недоступна у поточному потоці.')
    result = command(['set_property', prop, value], timeout=3.0)
    if result.get('error') not in (None, 'success'):
        raise RuntimeError(f'MPV не застосував доріжку: {result.get("error")}')
    time.sleep(0.12)
    return {'track_kind': kind, 'track_id': value, 'player': status()}


def _is_live_stream_state(value: dict[str, Any] | None) -> bool:
    data = value if isinstance(value, dict) else {}
    return bool(
        data.get('live')
        or data.get('backend') == 'iptv'
        or data.get('channel_id')
        or data.get('channel_number') is not None
    )


def seek_absolute(position: float) -> dict[str, Any]:
    active_backend, _detected = _active_backend()
    if active_backend == 'chrome':
        process = subprocess.run([BROWSER_MEDIA, 'seek', str(max(0.0, float(position)))], text=True, capture_output=True, timeout=6, check=False)
        if process.returncode:
            raise RuntimeError((process.stderr or process.stdout or 'Chrome не прийняв перемотування.').strip()[-500:])
        return {'result': {'error': 'success'}, 'player': status()}
    if active_backend == 'kiosk':
        process = subprocess.run([CHROME_MEDIA, 'seek', str(max(0.0, float(position)))], text=True, capture_output=True, timeout=4, check=False)
        if process.returncode:
            raise RuntimeError((process.stderr or process.stdout or 'YouTube Web не прийняв перемотування.').strip()[-500:])
        return {'result': {'error': 'success'}, 'player': status()}
    if not _player_active():
        raise RuntimeError('Плеєр не запущений.')
    current = status()
    if _is_live_stream_state(current):
        raise ValueError('Перемотування недоступне для прямого IPTV-ефіру.')
    position = max(0.0, float(position))
    duration = current.get('duration')
    if isinstance(duration, (int, float)) and duration > 0:
        position = min(position, float(duration))
    result = command(['seek', position, 'absolute+exact'])
    if result.get('error') not in (None, 'success'):
        raise RuntimeError(str(result.get('error')))
    time.sleep(0.15)
    return {'result': result, 'player': status()}


def control(action: str) -> dict[str, Any]:
    active_backend, _detected = _active_backend()
    if active_backend == 'chrome':
        process = subprocess.run([BROWSER_MEDIA, 'control', action], text=True, capture_output=True, timeout=7, check=False)
        if process.returncode:
            raise RuntimeError((process.stderr or process.stdout or 'Chrome не прийняв команду.').strip()[-500:])
        try:
            return json.loads(process.stdout or '{}')
        except json.JSONDecodeError:
            return {'result': {'error': 'success'}, 'player': status()}
    if active_backend == 'kiosk':
        mapping = {'pause': 'pause', 'play': 'play', 'toggle': 'toggle', 'back': 'back', 'forward': 'forward', 'stop': 'stop'}
        command_name = mapping.get(action)
        if not command_name:
            raise ValueError('Невідома команда')
        process = subprocess.run([CHROME_MEDIA, 'control', command_name], text=True, capture_output=True, timeout=4, check=False)
        if process.returncode:
            raise RuntimeError((process.stderr or process.stdout or 'YouTube Web не прийняв команду.').strip()[-500:])
        return {'result': {'error': 'success'}, 'player': status()}
    if not _player_active():
        raise RuntimeError('Плеєр не запущений.')
    current = status()
    if action in {'pause', 'play', 'toggle', 'back', 'forward'} and _is_live_stream_state(current):
        raise ValueError('Пауза й перемотування недоступні для прямого IPTV-ефіру без підтвердженого timeshift.')
    if action == 'pause':
        result = command(['set_property', 'pause', True])
    elif action == 'play':
        result = command(['set_property', 'pause', False])
    elif action == 'toggle':
        result = command(['cycle', 'pause'])
    elif action == 'back':
        result = command(['seek', -15, 'relative'])
    elif action == 'forward':
        result = command(['seek', 15, 'relative'])
    elif action == 'stop':
        stop()
        try:
            _start_user_unit(TV_MODE_UNIT_TEMPLATE.format('kiosk'))
        except Exception:
            pass
        result = {'error': 'success'}
    else:
        raise ValueError('Невідома команда')
    return {'result': result, 'player': status()}


def _named_process_running(name: str) -> bool:
    process = subprocess.run(
        ['/usr/bin/pgrep', '-u', '1000', '-x', name],
        text=True, capture_output=True, timeout=3, check=False,
    )
    return process.returncode == 0


def _game_runtime_running() -> bool:
    process = subprocess.run(
        ['/usr/bin/pgrep', '-u', '1000', '-f',
         rf'^python3 {re.escape(str(HOME / ".local/bin/home-edge-game-library"))}$|^/usr/bin/(fuse|fuse-sdl)( |$)'],
        text=True, capture_output=True, timeout=3, check=False,
    )
    return process.returncode == 0



def _focus_kiosk_window() -> bool:
    if not Path(XDOTOOL).exists():
        return False
    auth_files = sorted(
        Path('/run/user/1000').glob('.mutter-Xwaylandauth.*'),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not auth_files:
        return False
    for display in (':0', ':1', ':2'):
        env = os.environ.copy()
        env.update({
            'HOME': str(HOME),
            'DISPLAY': display,
            'XAUTHORITY': str(auth_files[0]),
            'XDG_RUNTIME_DIR': '/run/user/1000',
            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus',
        })
        found = subprocess.run(
            [XDOTOOL, 'search', '--onlyvisible', '--name', 'YouTube.*Google Chrome'],
            text=True, capture_output=True, timeout=4, check=False, env=env,
        )
        windows = [line.strip() for line in found.stdout.splitlines() if line.strip().isdigit()]
        for window in reversed(windows):
            focused = subprocess.run(
                [XDOTOOL, 'windowactivate', '--sync', window],
                text=True, capture_output=True, timeout=4, check=False, env=env,
            )
            if focused.returncode == 0:
                return True
    return False


def _activate_kiosk_in_place() -> dict[str, Any]:
    process = subprocess.run(
        [CHROME_MEDIA, 'activate'], text=True, capture_output=True,
        timeout=5, check=False,
    )
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout or 'YouTube Web не вдалося активувати.').strip()[-600:])
    try:
        result = json.loads(process.stdout or '{}')
    except json.JSONDecodeError as exc:
        raise RuntimeError('YouTube Web повернув некоректну відповідь активації.') from exc
    focused = _focus_kiosk_window()
    deadline = time.time() + 8.0
    last_error = ''
    while time.time() < deadline:
        probe = subprocess.run(
            [CHROME_MEDIA, 'status'], text=True, capture_output=True,
            timeout=3, check=False,
        )
        if probe.returncode == 0:
            try:
                status_data = json.loads(probe.stdout or '{}')
            except json.JSONDecodeError:
                status_data = {}
            if str(status_data.get('url') or '').startswith('https://www.youtube.com/tv'):
                return {
                    'focused': focused,
                    'browser_action': result.get('action'),
                    'preserved_playback': bool(result.get('preserved_playback')),
                    'url': status_data.get('url'),
                }
        last_error = (probe.stderr or probe.stdout or '').strip()[-300:]
        time.sleep(0.25)
    raise RuntimeError('YouTube Web не підтвердив готовність після активації: ' + last_error)



def _iptv_transition_active() -> bool:
    try:
        data = json.loads(IPTV_TRANSITION.read_text(encoding='utf-8'))
        started = float(data.get('started_at') or 0.0)
        return (
            data.get('schema') == 'skeleton.iptv.transition.v1'
            and bool(data.get('target_channel_id'))
            and 0.0 <= time.time() - started <= 120.0
        )
    except Exception:
        return False

def mode_status(*, include_transition: bool = True) -> dict[str, Any]:
    tty = _active_tty()
    raw = _current_tv_mode()
    detected_backend, detected_state = _active_backend()
    requested_target = _mode_transition_target() if include_transition else None
    if requested_target is not None:
        mode = requested_target
    # An IPTV handoff is an atomic user-visible TV transition. IPTV reuses the
    # MPV backend internally, so exposing raw=mpv while the new live stream is
    # being prepared causes a false intermediate "Video" mode in Home.
    # The transition marker is written only after a concrete target channel is
    # resolved and is bounded by _iptv_transition_active().
    elif _iptv_transition_active():
        mode = 'tv'
    elif isinstance(detected_state, dict) and detected_backend in {'chrome', 'kiosk'} and (detected_state.get('running') or detected_backend == 'kiosk' and _youtube_page_ready(detected_state)):
        mode = detected_backend
    elif raw in {'chrome', 'kiosk'}:
        mode = raw if _named_process_running('chrome') else 'unknown'
    elif raw == 'games':
        mode = 'games' if _game_runtime_running() else 'unknown'
    elif raw == 'mpv':
        try:
            current = json.loads(CURRENT.read_text(encoding='utf-8'))
        except Exception:
            current = {}
        mode = 'tv' if (_player_active() and (_iptv_transition_active() or current.get('backend') == 'iptv')) else 'mpv'
    elif raw == 'off':
        mode = 'off'
    elif tty == 'tty2':
        if _player_active():
            mode = 'mpv'
        elif _named_process_running('chrome'):
            mode = 'kiosk'
        else:
            mode = 'unknown'
    else:
        mode = 'unknown'
    if mode in {'chrome', 'kiosk', 'mpv', 'games', 'off'} and mode != raw and not MODE_REQUEST_FILE.exists():
        try:
            MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
            MODE_FILE.write_text(mode + '\n', encoding='utf-8')
            raw = mode
        except OSError:
            pass
    return {'mode': mode, 'tv_mode': raw, 'active_tty': tty, 'detected_backend': detected_backend if mode != raw else None}


def switch_mode(mode: str) -> dict[str, Any]:
    # GNOME may boot or remain in Activities/Overview. In that state both warm
    # media surfaces are shown as window thumbnails even when each is fullscreen.
    # Always leave Overview before applying a TV media mode.
    _exit_gnome_overview()
    normalized = {
        'media': 'mpv', 'cast': 'mpv', 'video': 'mpv',
        'youtube': 'kiosk', 'youtube_tv': 'kiosk',
        'web': 'chrome',
    }.get(mode, mode)
    allowed = {'chrome', 'kiosk', 'mpv', 'games', 'off'}
    if normalized not in allowed:
        raise ValueError('Невідомий відеорежим.')

    current = mode_status(include_transition=False)
    if normalized == 'mpv':
        return restore_last_vod()
    if normalized in {'kiosk', 'chrome', 'games', 'off'}:
        _set_display_refresh(60)
    same_live_mode = current.get('mode') == normalized
    if same_live_mode:
        if normalized == 'kiosk':
            # A live/IPTV MPV surface must never coexist with the YouTube kiosk.
            # If an orphan MPV survives without its socket/PID state, re-run the
            # canonical kiosk mode entrypoint so its robust stop_mpv() cleanup
            # removes the competing surface before YouTube is re-focused.
            if _named_process_running('mpv') and not _player_active():
                _start_user_unit(TV_MODE_UNIT_TEMPLATE.format('kiosk'))
            if _player_active():
                try:
                    active_record = json.loads(CURRENT.read_text(encoding='utf-8'))
                except Exception:
                    active_record = {}
                if active_record.get('live') or active_record.get('backend') == 'iptv':
                    stop()
                    CURRENT.unlink(missing_ok=True)
                    IPTV_TRANSITION.unlink(missing_ok=True)
            activation = _activate_kiosk_in_place()
            if _player_active():
                try:
                    active_record = json.loads(CURRENT.read_text(encoding='utf-8'))
                except Exception:
                    active_record = {}
                if active_record.get('live') or active_record.get('backend') == 'iptv':
                    raise RuntimeError('YouTube kiosk activated while live TV backend remained active')
            return {**mode_status(), 'unchanged': True, 'transition': 'in-place-refresh', 'activation': activation}
        return {**current, 'unchanged': True, 'transition': 'none'}

    parked_vod = False
    if current.get('mode') == 'mpv':
        suspended = suspend_current_vod('before_mode_switch')
        parked_vod = bool(suspended.get('suspended')) and normalized in {'kiosk', 'chrome'}
        if not parked_vod:
            stop()
    elif current.get('mode') == 'tv':
        # Live TV is stopped, never parked in a growing paused buffer.
        stop()
    if normalized not in {'kiosk', 'chrome'}:
        CURRENT.unlink(missing_ok=True)
    _start_user_unit(TV_MODE_UNIT_TEMPLATE.format(normalized))
    if parked_vod and normalized == 'kiosk':
        transition = 'warm-vod-to-kiosk'
    elif parked_vod and normalized == 'chrome':
        transition = 'warm-vod-to-chrome'
    else:
        transition = 'warm-kiosk-restore' if normalized == 'kiosk' and _named_process_running('chrome') else 'mode-switch'
    return {**mode_status(), 'unchanged': False, 'transition': transition, 'parked_vod': parked_vod}
