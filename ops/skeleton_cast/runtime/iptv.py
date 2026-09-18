from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import player

HOME = Path(os.environ.get('SKELETON_MEDIA_HOME', str(Path.home()))).expanduser()
BASE = HOME / '.local/lib/skeleton-cast'
CATALOG = HOME / '.local/share/skeleton/iptv/verified-channels.json'
STATE = HOME / '.local/state/skeleton-cast/tv-state.json'
TRANSITION = HOME / '.local/state/skeleton-cast/tv-transition.json'
PREFERENCES = HOME / '.local/state/skeleton-cast/tv-channel-preferences.json'
REFRESH_STATE = HOME / '.local/state/skeleton-cast/iptv-refresh.json'
EPG_STATE = HOME / '.local/state/skeleton-cast/iptv-epg.json'
FORCE_MARKER = HOME / '.local/state/skeleton-cast/iptv-refresh-force'
PICON_DIR = BASE / 'static/iptv-picons'
REFRESH_UNIT = 'home-edge-iptv-refresh.service'
SYSTEMCTL = '/usr/bin/systemctl'
_LOCK = threading.RLock()


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _catalog() -> dict[str, Any]:
    data = _read(CATALOG)
    channels = data.get('channels')
    if data.get('schema') != 'skeleton.iptv.verified_catalog.v1' or not isinstance(channels, list) or not channels:
        raise RuntimeError('Перевірений каталог IPTV недоступний.')
    return data


def _normal(value: Any) -> str:
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    text = re.sub(r'\([^)]*(?:\d{3,4}p|hd|sd|uhd|4k)[^)]*\)', ' ', text)
    text = re.sub(r'\b(?:\d{3,4}p|hd|sd|uhd|4k)\b', ' ', text)
    return re.sub(r'[^0-9a-zа-яіїєґ]+', ' ', text).strip()


def stable_key(channel: dict[str, Any]) -> str:
    tvg_id = str(channel.get('tvg_id') or '').strip()
    if tvg_id:
        return 'tvg:' + tvg_id.split('@', 1)[0].casefold()
    material = _normal(channel.get('name')) + '|' + _normal(channel.get('group'))
    return 'name:' + hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]


def channel_id(channel: dict[str, Any]) -> str:
    return hashlib.sha256(stable_key(channel).encode('utf-8')).hexdigest()[:16]


def _raw_channels() -> list[dict[str, Any]]:
    return [dict(item) for item in _catalog().get('channels', []) if isinstance(item, dict)]


def _preferences() -> dict[str, Any]:
    data = _read(PREFERENCES)
    order = data.get('order') if isinstance(data.get('order'), list) else []
    hidden = data.get('hidden') if isinstance(data.get('hidden'), list) else []
    return {
        'schema': 'skeleton.iptv.channel_preferences.v1',
        'order': [str(item) for item in order if isinstance(item, str)],
        'hidden': [str(item) for item in hidden if isinstance(item, str)],
        'updated_at': data.get('updated_at'),
    }


def _ordered_channels(include_hidden: bool = False) -> list[dict[str, Any]]:
    rows = _raw_channels()
    prefs = _preferences()
    order = list(dict.fromkeys(prefs['order']))
    order_index = {key: index for index, key in enumerate(order)}
    hidden = set(prefs['hidden'])
    decorated: list[dict[str, Any]] = []
    for original_index, item in enumerate(rows):
        key = stable_key(item)
        current = dict(item)
        current['stable_key'] = key
        current['legacy_channel_id'] = str(item.get('channel_id') or '')
        current['channel_id'] = channel_id(current)
        current['catalog_number'] = item.get('number')
        current['_catalog_index'] = original_index
        current['visible'] = key not in hidden
        decorated.append(current)
    decorated.sort(key=lambda item: (
        0 if item['stable_key'] in order_index else 1,
        order_index.get(item['stable_key'], 10**9),
        int(item.get('_catalog_index') or 0),
        _normal(item.get('name')),
    ))
    visible_number = 0
    for position, item in enumerate(decorated, 1):
        item['position'] = position
        if item['visible']:
            visible_number += 1
            item['number'] = visible_number
        else:
            item['number'] = None
    result = decorated if include_hidden else [item for item in decorated if item['visible']]
    for item in result:
        item.pop('_catalog_index', None)
    return result


def _display_name(channel: dict[str, Any]) -> str:
    text = ' '.join(str(channel.get('name') or 'Канал').split()).strip()
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r'\s*\((?:\d{3,4}[pi]|hd|sd|uhd|4k)\)\s*$', '', text, flags=re.I)
        text = re.sub(r'\s*\[(?:geo-blocked|not 24/7|offline|online only)[^]]*\]\s*$', '', text, flags=re.I)
    return text.strip() or 'Канал'


def _epg() -> dict[str, Any]:
    data = _read(EPG_STATE)
    return data if data.get('schema') == 'skeleton.iptv.epg.v1' else {}


def _programme(channel: dict[str, Any]) -> dict[str, Any]:
    base = str(channel.get('tvg_id') or '').split('@', 1)[0].strip().casefold()
    item = ((_epg().get('programmes') or {}).get(base) or {}) if base else {}
    current = item.get('current') if isinstance(item.get('current'), dict) else {}
    following = item.get('next') if isinstance(item.get('next'), dict) else {}
    return {
        'programme_title': current.get('title'),
        'programme_start': current.get('start'),
        'programme_stop': current.get('stop'),
        'next_programme_title': following.get('title'),
        'next_programme_start': following.get('start'),
        'epg_available': bool(current),
    }


def _picon_url(channel: dict[str, Any]) -> str | None:
    target = PICON_DIR / f'{channel_id(channel)}.png'
    if not target.is_file():
        return None
    try:
        stamp = int(target.stat().st_mtime)
    except OSError:
        stamp = 0
    return f'/static/iptv-picons/{target.name}?v={stamp}'


def _public(channel: dict[str, Any]) -> dict[str, Any]:
    value = {
        key: channel.get(key)
        for key in (
            'channel_id', 'stable_key', 'number', 'catalog_number', 'position', 'visible',
            'name', 'tvg_id', 'group', 'adaptive', 'selected_height', 'audio_codec',
            'video_codec', 'required_mbps', 'startup_seconds', 'repository_present',
        )
    }
    value['display_name'] = _display_name(channel)
    value['picon'] = _picon_url(channel)
    value.update(_programme(channel))
    return value


def public_catalog() -> dict[str, Any]:
    data = _catalog()
    channels = _ordered_channels(False)
    total = len(_ordered_channels(True))
    prefs = _preferences()
    return {
        'schema': data.get('schema'),
        'generated_at': data.get('generated_at'),
        'source_repository': data.get('source_repository'),
        'source_playlist': data.get('source_playlist'),
        'safe_budget_mbps': data.get('safe_budget_mbps'),
        'hls_bitrate_cap_bps': data.get('hls_bitrate_cap_bps'),
        'channel_count': len(channels),
        'total_channel_count': total,
        'preferences_updated_at': prefs.get('updated_at'),
        'epg': {key: _epg().get(key) for key in ('generated_at', 'mapped_channels', 'current_programmes')},
        'channels': [_public(item) for item in channels],
    }


def editor_catalog() -> dict[str, Any]:
    data = _catalog()
    channels = _ordered_channels(True)
    prefs = _preferences()
    return {
        'schema': 'skeleton.iptv.channel_editor.v1',
        'generated_at': data.get('generated_at'),
        'source_repository': data.get('source_repository'),
        'source_playlist': data.get('source_playlist'),
        'preferences_updated_at': prefs.get('updated_at'),
        'visible_count': sum(bool(item.get('visible')) for item in channels),
        'total_count': len(channels),
        'channels': [_public(item) for item in channels],
        'refresh': refresh_status(),
    }


def save_preferences(order: list[Any], visible: list[Any]) -> dict[str, Any]:
    with _LOCK:
        all_channels = _ordered_channels(True)
        known = {str(item['stable_key']) for item in all_channels}
        if not isinstance(order, list) or not isinstance(visible, list):
            raise ValueError('Порядок і видимість каналів мають бути списками.')
        clean_order = [str(item) for item in order]
        clean_visible = [str(item) for item in visible]
        if len(clean_order) != len(set(clean_order)) or len(clean_visible) != len(set(clean_visible)):
            raise ValueError('Список каналів містить дублікати.')
        if set(clean_order) - known or set(clean_visible) - known:
            raise ValueError('Список містить невідомий канал; оновіть редактор.')
        if not clean_visible:
            raise ValueError('Має залишитися хоча б один видимий канал.')
        current_order = [str(item['stable_key']) for item in all_channels]
        missing = [key for key in current_order if key not in clean_order]
        previous = _preferences()
        retired = [key for key in previous['order'] if key not in known]
        final_order = clean_order + missing + [key for key in retired if key not in clean_order and key not in missing]
        payload = {
            'schema': 'skeleton.iptv.channel_preferences.v1',
            'order': final_order,
            'hidden': sorted(known - set(clean_visible)),
            'updated_at': int(time.time()),
        }
        _write(PREFERENCES, payload)
        return editor_catalog()


def _find(channel_ref: str | None, visible_only: bool = False) -> dict[str, Any]:
    channels = _ordered_channels(not visible_only)
    if channel_ref:
        needle = str(channel_ref)
        found = next((item for item in channels if needle in {
            str(item.get('channel_id') or ''),
            str(item.get('stable_key') or ''),
            str(item.get('legacy_channel_id') or ''),
        }), None)
        if found:
            return found
    stored = _read(STATE)
    stored_key = str(stored.get('stable_key') or '')
    stored_id = str(stored.get('channel_id') or '')
    found = next((item for item in channels if item.get('stable_key') == stored_key or item.get('channel_id') == stored_id or item.get('legacy_channel_id') == stored_id), None)
    if found:
        return found
    espreso = next((item for item in channels if stable_key(item) == 'tvg:espresotv.ua'), None)
    return espreso or channels[0]


def _source(channel: dict[str, Any]) -> dict[str, Any]:
    headers = {'User-Agent': str(channel.get('user_agent') or player.UA)}
    return {
        'source_id': str(channel.get('channel_id') or channel_id(channel)),
        'url': str(channel.get('stream_url')),
        'kind': 'iptv',
        'backend': 'iptv',
        'live': True,
        'channel_id': str(channel.get('channel_id') or channel_id(channel)),
        'stable_key': stable_key(channel),
        'channel_number': channel.get('number'),
        'title': channel.get('name'),
        'page_title': channel.get('name'),
        'quality': f"TV · {int(channel.get('selected_height') or 0)}p" if channel.get('selected_height') else 'TV · Auto',
        'translation': 'Прямий ефір',
        'episode': 'Live',
        'poster': _picon_url(channel),
        'duration': None,
        'height': channel.get('selected_height'),
        'headers': headers,
        'has_audio': True,
        'has_drm': False,
        'hls_bitrate': int(channel.get('hls_bitrate_cap_bps') or 3_500_000),
    }


def play_channel(channel_ref: str | None = None) -> dict[str, Any]:
    with _LOCK:
        # A seekable cast/VOD is paused and checkpointed before MPV is reused
        # for live TV. Live-to-live channel changes simply replace the stream.
        player.suspend_current_vod('before_iptv')
        channel = _find(channel_ref, visible_only=False) if channel_ref else _find(None, visible_only=True)
        source = _source(channel)
        job = {
            'job_id': None,
            'status': 'ready',
            'title': str(channel.get('name') or 'TV'),
            'page_url': str(channel.get('source_playlist') or ''),
            'site_host': 'iptv-org',
            'poster': _picon_url(channel),
            'sources': [source],
            'live': True,
            'backend': 'iptv',
        }
        target_id = str(channel.get('channel_id') or channel_id(channel))
        _write(TRANSITION, {
            'schema': 'skeleton.iptv.transition.v1',
            'target_channel_id': target_id,
            'target_stable_key': stable_key(channel),
            'started_at': time.time(),
        })
        try:
            result = player.play(job, source, 'off')
            record = {
                'schema': 'skeleton.iptv.state.v2',
                'stable_key': stable_key(channel),
                'channel_id': target_id,
                'number': channel.get('number'),
                'name': channel.get('name'),
                'picon': _picon_url(channel),
                'selected_height': channel.get('selected_height'),
                'adaptive': channel.get('adaptive'),
                'hls_bitrate_cap_bps': channel.get('hls_bitrate_cap_bps'),
                'updated_at': int(time.time()),
                'pid': result.get('pid') or result.get('pid_after'),
            }
            _write(STATE, record)
            return {'tv': record, **result}
        finally:
            TRANSITION.unlink(missing_ok=True)


def step(delta: int) -> dict[str, Any]:
    channels = _ordered_channels(False)
    if not channels:
        raise RuntimeError('Список видимих каналів порожній.')
    current_state = _read(STATE)
    current_key = str(current_state.get('stable_key') or '')
    current_id = str(current_state.get('channel_id') or '')
    index = next((i for i, item in enumerate(channels) if item.get('stable_key') == current_key or item.get('channel_id') == current_id), None)
    if index is None:
        index = -1 if delta > 0 else 0
    target = channels[(index + (1 if delta > 0 else -1)) % len(channels)]
    return play_channel(str(target.get('channel_id')))


def status() -> dict[str, Any]:
    catalog = _catalog()
    stored = _read(STATE)
    current = _read(player.CURRENT)
    active = player.mode_status().get('mode') == 'tv'
    current_key = str(current.get('stable_key') or stored.get('stable_key') or '')
    current_id = str(current.get('channel_id') or stored.get('channel_id') or '')
    channel = next((item for item in _ordered_channels(True) if item.get('stable_key') == current_key or item.get('channel_id') == current_id or item.get('legacy_channel_id') == current_id), None)
    visible = _ordered_channels(False)
    return {
        'active': active,
        'channel': _public(channel) if isinstance(channel, dict) else None,
        'channel_count': len(visible),
        'total_channel_count': len(_ordered_channels(True)),
        'safe_budget_mbps': catalog.get('safe_budget_mbps'),
        'hls_bitrate_cap_bps': catalog.get('hls_bitrate_cap_bps'),
        'refresh': refresh_status(),
        'player': player.status() if active else {'running': False},
    }


def refresh_status() -> dict[str, Any]:
    data = _read(REFRESH_STATE)
    return {
        key: data.get(key)
        for key in (
            'status', 'checked_at', 'changed_at', 'playlist_etag', 'logos_etag',
            'catalog_changed', 'added', 'updated', 'removed', 'failed',
            'picons_cached', 'error', 'running_since',
        )
        if key in data
    }


def request_refresh(force: bool = False) -> dict[str, Any]:
    if force:
        FORCE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        FORCE_MARKER.write_text(str(int(time.time())) + '\n', encoding='ascii')
        os.chmod(FORCE_MARKER, 0o600)
    args = [SYSTEMCTL, '--user', 'start', '--no-block', REFRESH_UNIT]
    result = subprocess.run(args, text=True, capture_output=True, timeout=8, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or 'Не вдалося запустити перевірку IPTV.').strip()[-500:])
    return {'accepted': True, 'force': force, 'refresh': refresh_status()}


def start_default() -> dict[str, Any]:
    return play_channel(None)
