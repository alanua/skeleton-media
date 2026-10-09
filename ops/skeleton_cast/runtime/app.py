#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import ipaddress
import math
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import yaml
from flask import Flask, Response, abort, jsonify, request, send_from_directory

import media_search
import native_app_update_manifest
import player
import site_registry
from resolver import BrowserChallengeError, OriginProtectedError, resolve_page

HOME = Path(os.environ.get('SKELETON_MEDIA_HOME', str(Path.home()))).expanduser()
BASE = HOME / '.local/lib/skeleton-cast'
STATE = HOME / '.local/state/skeleton-cast'
HOME_NATIVE_RELEASE = STATE / 'home-native-release.json'
JOBS = STATE / 'jobs'
STATIC = BASE / 'static'
POSTERS = STATE / 'posters'
REGISTRY = HOME / '.config/skeleton/device-registry/confirmed.yaml'
LAN_HOST = os.environ.get('SKELETON_MEDIA_LAN_HOST', '127.0.0.1')
PORT = 8100
VOLUME_POLICY = HOME / '.local/bin/home-edge-volume-policy'
VOLUME_STATE = HOME / '.local/state/skeleton/volume-policy.json'
HOME_EDGE_CONTROL = HOME / '.local/bin/home-edge-control'
SYSTEMCTL = '/usr/bin/systemctl'
SYSTEMD_RUN = '/usr/bin/systemd-run'
GAME_INPUT = '/usr/local/sbin/home-edge-game-input'
GAME_STATE = HOME / '.local/state/home-edge-games/state.json'
GAME_INPUT_SOCKET = Path('/run/skeleton/home-edge-game-input.sock')
POINTER_INPUT_SOCKET = Path('/run/user/1000/skeleton-pointer.sock')
TV_MODE = HOME / '.local/bin/tv-mode'
XDOTOOL = '/usr/bin/xdotool'
CHROME_MEDIA = HOME / '.local/bin/home-edge-chrome-media'
ANDROID_SERIAL = os.environ.get('SKELETON_MEDIA_ANDROID_SERIAL', '')
ALLOWED = ('uakino.club', 'uakino.me', 'uakino.best', 'klon.fun', 'ashdi.vip')
URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='skeleton-cast')
_MODE_LOCK = threading.Lock()
_REMOTE_IME_LOCK = threading.Lock()
_REMOTE_IME_CACHE: dict[str, object] = {'at': 0.0, 'shown': False, 'context': None}
_REMOTE_POINTER_LOCK = threading.Lock()
_REMOTE_DRAG_TIMER: threading.Timer | None = None
_REMOTE_DRAG_MODE: str | None = None
_ANDROID_POINTER = [960, 540]
_ANDROID_SIZE_CACHE: dict[str, object] = {'at': 0.0, 'size': (1920, 1080)}
_DESKTOP_TARGET_LOCK = threading.Lock()
_DESKTOP_TARGET_CACHE: dict[str, object] = {'at': 0.0, 'mode': None, 'env': None, 'window': None}
_REMOTE_STATUS_CACHE_LOCK = threading.Lock()
_REMOTE_STATUS_CACHE: dict[str, object] = {'at': 0.0, 'mode': None, 'data': None, 'refreshing': False}
_MEDIA_SEARCH_BREAKER = media_search.CircuitBreaker()
_MEDIA_RELEASE_TRACKING = media_search.ReleaseTrackingStore()
MEDIA_TARGET_STATE = STATE / 'media-target.json'
SAMSUNG_MEDIA_DESIRED = STATE / 'samsung-media-desired.json'
SAMSUNG_MEDIA_STATUS = STATE / 'samsung-media-status.json'
SAMSUNG_RECEIVER_DESIRED = SAMSUNG_MEDIA_DESIRED
SAMSUNG_RECEIVER_STATUS = SAMSUNG_MEDIA_STATUS
WATCH_HISTORY_LEGACY = STATE / 'watch-history.json'
WATCH_HISTORY_ENDPOINTS = STATE / 'watch-history-endpoints.json'
WATCH_HISTORY_LEGACY_SCHEMA = 'skeleton.media.watch_history.v2'
WATCH_HISTORY_ENDPOINT_SCHEMA = 'skeleton.media.endpoint_watch_history.v1'
_ENDPOINT_HISTORY_LOCK = getattr(player, 'ENDPOINT_HISTORY_LOCK', threading.RLock())
_ENDPOINT_HISTORY_CACHE_LOCK = threading.RLock()
_ENDPOINT_HISTORY_CACHE: dict[str, object] = {'path': None, 'signature': None, 'store': None}
HOME_EDGE_TV_HISTORY_ID = 'home_edge_tv'
SAMSUNG_DEVICE_ID = 'samsung_kiosk'
TARGET_LABELS = {'tv': 'TV', 'samsung': 'Samsung Kiosk'}
MEDIA_TARGET_POSITION_TOLERANCE_SECONDS = 12.0
MEDIA_TARGET_WAIT_SECONDS = 8.0
_CLEAR_CHARGE = object()


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def _job_path(job_id: str) -> Path:
    if not re.fullmatch(r'[0-9a-f]{16}', job_id):
        abort(404)
    return JOBS / f'{job_id}.json'


def _load(job_id: str) -> dict:
    path = _job_path(job_id)
    if not path.exists():
        abort(404)
    return json.loads(path.read_text(encoding='utf-8'))


def _save(job: dict) -> None:
    _atomic(_job_path(job['job_id']), job)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _stable_history_id(value: object) -> str:
    history_id = str(value or '').strip().lower()
    history_id = re.sub(r'[^a-z0-9_:-]+', '_', history_id).strip('_')
    if not history_id:
        raise ValueError('Невідомий медіа-екран.')
    return history_id


def _history_items_from_payload(data: dict) -> list[dict]:
    for key in ('items', 'history', 'entries'):
        value = data.get(key)
        if isinstance(value, dict):
            items = []
            for item_key, raw in value.items():
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                item.setdefault('content_key', str(item_key))
                item.setdefault('history_key', str(item_key))
                if not item.get('job_id') and item.get('last_job_id'):
                    item['job_id'] = item.get('last_job_id')
                if not item.get('source_id') and item.get('last_source_id'):
                    item['source_id'] = item.get('last_source_id')
                if not item.get('title') and item.get('display_title'):
                    item['title'] = item.get('display_title')
                items.append(item)
            return items
        if isinstance(value, list):
            items = []
            for raw in value:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                if not item.get('job_id') and item.get('last_job_id'):
                    item['job_id'] = item.get('last_job_id')
                if not item.get('source_id') and item.get('last_source_id'):
                    item['source_id'] = item.get('last_source_id')
                if not item.get('title') and item.get('display_title'):
                    item['title'] = item.get('display_title')
                items.append(item)
            return items
    return []


def _legacy_samsung_owned(item: dict) -> bool:
    return str(item.get('last_reason') or item.get('reason') or '').strip().lower() == 'samsung_periodic'


def _history_source_counts(legacy_items: list[dict]) -> dict:
    return {
        'legacy_total': len(legacy_items),
        HOME_EDGE_TV_HISTORY_ID: len(legacy_items),
        SAMSUNG_DEVICE_ID: sum(1 for item in legacy_items if _legacy_samsung_owned(item)),
    }


def _history_default(legacy_items: list[dict] | None = None) -> dict:
    legacy_items = list(legacy_items or [])
    return {
        'schema': WATCH_HISTORY_ENDPOINT_SCHEMA,
        'version': 1,
        'updated_at': int(time.time()),
        'histories': {
            HOME_EDGE_TV_HISTORY_ID: {'items': [dict(item) for item in legacy_items]},
            SAMSUNG_DEVICE_ID: {'items': [dict(item) for item in legacy_items if _legacy_samsung_owned(item)]},
        },
        'migration': {
            'schema': WATCH_HISTORY_LEGACY_SCHEMA,
            'version': 1,
            'source': str(WATCH_HISTORY_LEGACY),
            'source_counts': _history_source_counts(legacy_items),
        },
    }


def _endpoint_history_store_path() -> Path:
    return Path(getattr(player, 'WATCH_HISTORY_ENDPOINTS', WATCH_HISTORY_ENDPOINTS))


def _endpoint_history_file_signature(path: Path | None = None) -> tuple[str, bool, int | None, int | None]:
    path = Path(path or _endpoint_history_store_path())
    try:
        stat = path.stat()
        return (str(path), True, stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        return (str(path), False, None, None)
    except Exception:
        return (str(path), False, None, None)


def _copy_history_store(store: dict) -> dict:
    return copy.deepcopy(store)


def _endpoint_history_cache_get(signature: tuple[str, bool, int | None, int | None]) -> dict | None:
    with _ENDPOINT_HISTORY_CACHE_LOCK:
        if (
            _ENDPOINT_HISTORY_CACHE.get('signature') == signature
            and isinstance(_ENDPOINT_HISTORY_CACHE.get('store'), dict)
        ):
            return _copy_history_store(_ENDPOINT_HISTORY_CACHE['store'])
    return None


def _endpoint_history_cache_set(signature: tuple[str, bool, int | None, int | None], store: dict) -> None:
    with _ENDPOINT_HISTORY_CACHE_LOCK:
        _ENDPOINT_HISTORY_CACHE['path'] = signature[0]
        _ENDPOINT_HISTORY_CACHE['signature'] = signature
        _ENDPOINT_HISTORY_CACHE['store'] = _copy_history_store(store)


def _endpoint_history_cache_clear() -> None:
    with _ENDPOINT_HISTORY_CACHE_LOCK:
        _ENDPOINT_HISTORY_CACHE['path'] = None
        _ENDPOINT_HISTORY_CACHE['signature'] = None
        _ENDPOINT_HISTORY_CACHE['store'] = None


def _load_endpoint_history_store() -> dict:
    path = _endpoint_history_store_path()
    signature = _endpoint_history_file_signature(path)
    cached = _endpoint_history_cache_get(signature)
    if cached is not None:
        return cached
    loader = getattr(player, '_load_endpoint_watch_history', None)
    if callable(loader):
        store = loader()
        if isinstance(store, dict):
            _endpoint_history_cache_set(_endpoint_history_file_signature(path), store)
            return _copy_history_store(store)
        return {}
    store = _read_json(WATCH_HISTORY_ENDPOINTS)
    if store.get('schema') == WATCH_HISTORY_ENDPOINT_SCHEMA and isinstance(store.get('histories'), dict):
        _endpoint_history_cache_set(signature, store)
        return _copy_history_store(store)
    legacy = _read_json(WATCH_HISTORY_LEGACY)
    legacy_items = _history_items_from_payload(legacy) if legacy.get('schema') == WATCH_HISTORY_LEGACY_SCHEMA else []
    store = _history_default(legacy_items)
    _atomic(WATCH_HISTORY_ENDPOINTS, store)
    _endpoint_history_cache_set(_endpoint_history_file_signature(path), store)
    return _copy_history_store(store)


def _save_endpoint_history_store(store: dict) -> dict:
    _endpoint_history_cache_clear()
    saver = getattr(player, '_write_endpoint_watch_history', None)
    if callable(saver):
        saver(store)
        _endpoint_history_cache_clear()
        return store
    store['schema'] = WATCH_HISTORY_ENDPOINT_SCHEMA
    store['version'] = int(store.get('version') or 1)
    store['updated_at'] = int(time.time())
    histories = store.get('histories')
    if not isinstance(histories, dict):
        store['histories'] = {}
    _atomic(WATCH_HISTORY_ENDPOINTS, store)
    _endpoint_history_cache_clear()
    return store


def _history_id_aliases() -> dict[str, str]:
    aliases = {
        'tv': HOME_EDGE_TV_HISTORY_ID,
        HOME_EDGE_TV_HISTORY_ID: HOME_EDGE_TV_HISTORY_ID,
        'samsung': SAMSUNG_DEVICE_ID,
        SAMSUNG_DEVICE_ID: SAMSUNG_DEVICE_ID,
    }
    for endpoint_id, adapter in _endpoint_adapters().items():
        if 'history' not in adapter.capabilities:
            continue
        history_id = adapter.history_namespace()
        aliases[_stable_history_id(endpoint_id)] = history_id
        if adapter.device_id:
            aliases[_stable_history_id(adapter.device_id)] = history_id
        aliases[_stable_history_id(history_id)] = history_id
    return aliases


def _resolve_history_id(endpoint: object = None) -> str:
    requested = _stable_history_id(endpoint or HOME_EDGE_TV_HISTORY_ID)
    return _history_id_aliases().get(requested, requested)


def _history_bucket(store: dict, endpoint: object = None) -> dict:
    history_id = _resolve_history_id(endpoint)
    histories = store.setdefault('histories', {})
    bucket = histories.setdefault(history_id, {'items': []})
    if not isinstance(bucket, dict):
        bucket = {'items': []}
        histories[history_id] = bucket
    if not isinstance(bucket.get('items'), list):
        bucket['items'] = []
    return bucket


def _history_item_key(item: dict) -> str:
    for key in ('history_key', 'media_key', 'key', 'id'):
        value = str(item.get(key) or '').strip()
        if value:
            return value
    identity = {
        key: item.get(key)
        for key in (
            'job_id', 'source_id', 'media_id', 'page_url', 'url', 'title', 'media_title',
            'original_title', 'season', 'episode', 'translation', 'group',
        )
        if item.get(key) not in (None, '')
    }
    raw = json.dumps(identity or item, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def _history_public_items(endpoint: object = None) -> dict:
    store = _load_endpoint_history_store()
    history_id = _resolve_history_id(endpoint)
    bucket = _history_bucket(store, history_id)
    return {
        'schema': WATCH_HISTORY_ENDPOINT_SCHEMA,
        'history_id': history_id,
        'items': [dict(item, history_key=_history_item_key(item)) for item in bucket.get('items', []) if isinstance(item, dict)],
        'migration': store.get('migration') if isinstance(store.get('migration'), dict) else {},
    }


def _history_find(endpoint: object, history_key: object) -> dict | None:
    target_key = str(history_key or '').strip()
    if not target_key:
        return None
    for item in _history_public_items(endpoint).get('items', []):
        if _history_item_key(item) == target_key:
            return item
    return None


def _history_delete(endpoint: object, history_key: object) -> dict:
    with _ENDPOINT_HISTORY_LOCK:
        store = _load_endpoint_history_store()
        history_id = _resolve_history_id(endpoint)
        bucket = _history_bucket(store, history_id)
        target_key = str(history_key or '').strip()
        before = len(bucket['items'])
        bucket['items'] = [item for item in bucket['items'] if not isinstance(item, dict) or _history_item_key(item) != target_key]
        _save_endpoint_history_store(store)
        return {'history_id': history_id, 'deleted': before - len(bucket['items'])}

def _progress_history_key(job_id: object, source_id: object, media: dict | None = None) -> str:
    media = media if isinstance(media, dict) else {}
    identity = {
        'job_id': str(job_id or media.get('job_id') or ''),
        'source_id': str(source_id or media.get('source_id') or ''),
        'season': str(media.get('season') or ''),
        'episode': str(media.get('episode') or ''),
        'translation': str(media.get('translation') or media.get('group') or ''),
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def _record_history_progress(endpoint: object, media: dict, *, job: dict | None = None, source: dict | None = None, reason: str = 'progress') -> dict:
    with _ENDPOINT_HISTORY_LOCK:
        history_id = _resolve_history_id(endpoint)
        store = _load_endpoint_history_store()
        bucket = _history_bucket(store, history_id)
        job = job if isinstance(job, dict) else {}
        source = source if isinstance(source, dict) else {}
        job_id = media.get('job_id') or job.get('job_id')
        source_id = media.get('source_id') or source.get('source_id')
        key = _progress_history_key(job_id, source_id, {**source, **media})
        existing = next((item for item in bucket['items'] if isinstance(item, dict) and _history_item_key(item) == key), None)
        item = dict(existing or {})
        now = int(time.time())
        item.update({
            'schema': 'skeleton.media.watch_item.v1',
            'history_key': key,
            'job_id': job_id,
            'source_id': source_id,
            'title': job.get('title') or media.get('title') or media.get('display_title') or item.get('title'),
            'position_seconds': round(_playback_position(media), 3),
            'last_reason': reason,
            'updated_at': now,
        })
        if 'duration_seconds' in media:
            item['duration_seconds'] = float(media.get('duration_seconds') or 0.0)
        if 'playing' in media:
            item['playing'] = bool(media.get('playing'))
        for source_key in ('season', 'episode', 'translation', 'group', 'quality'):
            if source.get(source_key) not in (None, ''):
                item[source_key] = source.get(source_key)
        if existing is None:
            item.setdefault('created_at', now)
            bucket['items'].insert(0, item)
        else:
            existing.clear()
            existing.update(item)
        _save_endpoint_history_store(store)
        return {'history_id': history_id, 'item': dict(item)}

def _record_tv_history_from_status(status: dict | None = None, *, reason: str = 'tv_progress') -> dict:
    saver = getattr(player, 'save_current_progress', None)
    if callable(saver):
        saved = saver(reason)
        if isinstance(saved, dict):
            return {'history_id': HOME_EDGE_TV_HISTORY_ID, 'item': saved}
    status = status if isinstance(status, dict) else player.status()
    if not status.get('job_id') or not status.get('source_id'):
        return {'history_id': HOME_EDGE_TV_HISTORY_ID, 'skipped': True}
    try:
        job, source = _find_job_source(status.get('job_id'), status.get('source_id'))
    except Exception:
        job, source = {}, {}
    return _record_history_progress(HOME_EDGE_TV_HISTORY_ID, status, job=job, source=source, reason=reason)


def _history_resume(endpoint: object, history_key: object) -> dict:
    history_id = _resolve_history_id(endpoint)
    item = _history_find(history_id, history_key)
    if not item:
        raise ValueError('Запис історії не знайдено.')
    job, source = _find_job_source(item.get('job_id'), item.get('source_id'))
    position = float(item.get('position_seconds') or 0.0)
    adapter = next(
        (
            candidate for candidate in _endpoint_adapters().values()
            if 'history' in candidate.capabilities and candidate.history_namespace() == history_id
        ),
        None,
    )
    if adapter is None:
        raise ValueError('Медіа-екран не підтримує відновлення історії.')
    result = adapter.resume_from_history(item, job, source, position)
    payload = result if isinstance(result, dict) else {'result': result}
    return {'history_id': history_id, 'item': item, 'endpoint_id': adapter.endpoint_id, **payload}


HOME_EDGE_DEVICE_ID = 'home_edge_01'
MEDIA_CAPTURE_OWNER = HOME_EDGE_DEVICE_ID
TRUSTED_CLIENT_IDS = tuple(
    dict.fromkeys(
        [item.strip() for item in os.environ.get('SKELETON_MEDIA_TRUSTED_CLIENT_IDS', '').split(',') if item.strip()]
        + [SAMSUNG_DEVICE_ID]
    )
)


def _identity_values(value: object) -> list[dict]:
    if not value:
        return []
    if isinstance(value, dict):
        if any(key in value for key in ('ipv4', 'tailscale_ipv4', 'mac')):
            return [value]
        return [item for item in value.values() if isinstance(item, dict)]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _trusted_identity_records() -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    try:
        data = yaml.safe_load(REGISTRY.read_text(encoding='utf-8')) or {}
        devices = data.get('devices') or {}
        for device_id in tuple(dict.fromkeys([*TRUSTED_CLIENT_IDS, SAMSUNG_DEVICE_ID])):
            device = devices.get(device_id) or {}
            if not device.get('operator_confirmed'):
                continue
            role = device.get('role')
            if device_id != SAMSUNG_DEVICE_ID and role not in ('android_phone', 'ios_phone', 'tablet_kiosk'):
                continue
            identifiers = device.get('identifiers') or {}
            identity_sets = [identifiers]
            identity_sets.extend(_identity_values(device.get('lan_identities')))
            identity_sets.extend(_identity_values(identifiers.get('lan_identities')))
            for identity in identity_sets:
                ip = str(identity.get('ipv4') or '').strip()
                mac = str(identity.get('mac') or '').strip().lower()
                tailscale_ip = str(identity.get('tailscale_ipv4') or '').strip()
                if ip or mac or tailscale_ip:
                    records.append({'device_id': device_id, 'ipv4': ip, 'mac': mac, 'tailscale_ipv4': tailscale_ip})
    except Exception:
        pass
    return records


def _client_identities() -> list[tuple[str, str, str]]:
    return [(item['ipv4'], item['mac'], item['tailscale_ipv4']) for item in _trusted_identity_records()]


def _neighbor_mac(ip: str) -> str | None:
    try:
        p = subprocess.run(['ip', 'neigh', 'show', ip], text=True, capture_output=True, timeout=2, check=False)
        m = re.search(r'\blladdr\s+([0-9a-f:]{17})\b', p.stdout, re.I)
        return m.group(1).lower() if m else None
    except Exception:
        return None


def _trusted() -> bool:
    ip = request.remote_addr or ''
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback:
        return True
    observed_mac = _neighbor_mac(ip)
    return any(
        ip == expected_ip
        or (tailscale_ip and ip == tailscale_ip)
        or (observed_mac and expected_mac and observed_mac == expected_mac)
        for expected_ip, expected_mac, tailscale_ip in _client_identities()
    )


def _require() -> None:
    if not _trusted():
        abort(403, description='Доступ дозволено лише підтвердженим сімейним пристроям через домашню мережу або Tailscale.')


def _request_device_id() -> str:
    ip = request.remote_addr or ''
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return 'unknown'
    if addr.is_loopback:
        return HOME_EDGE_DEVICE_ID
    records = _trusted_identity_records()
    for record in records:
        if ip == record['ipv4'] or (record['tailscale_ipv4'] and ip == record['tailscale_ipv4']):
            return record['device_id']
    observed_mac = _neighbor_mac(ip)
    if observed_mac:
        for record in records:
            if record['mac'] and observed_mac == record['mac']:
                return record['device_id']
    return 'unknown'


def _page_url(text: str) -> tuple[str, str, str]:
    match = URL_RE.search(text or '')
    if not match:
        raise ValueError('У переданому тексті немає URL.')
    raw = match.group(0).rstrip('.,);]}>')
    url, host = site_registry.validate_public_url(raw)
    _, entry = site_registry.lookup(host)
    status = 'confirmed' if entry and entry.get('status') == 'confirmed' else 'unknown'
    return url, host, status


def _resolve_job(job_id: str) -> None:
    job = _load(job_id)
    try:
        result = resolve_page(job['page_url'])
        job.update(result)
        job.pop('error', None)
        job.pop('error_detail', None)
        job.pop('error_type', None)
        if job.get('site_status') != 'confirmed':
            job['site_candidate'] = site_registry.make_candidate(job['page_url'], result)
            job['site_status'] = 'candidate'
        job.update({'status': 'ready', 'resolved_at': int(time.time())})
    except Exception as exc:
        if isinstance(exc, OriginProtectedError):
            public_error = 'AniTube тимчасово захищений. Повторна перевірка буде доступна після завершення паузи.'
            detail = json.dumps({
                'type': 'origin_protected',
                'url': exc.url,
                'cooldown_remaining_seconds': exc.cooldown_remaining_seconds,
            }, ensure_ascii=False)
            error_type = 'origin_protected'
            job['cooldown_remaining_seconds'] = exc.cooldown_remaining_seconds
        elif isinstance(exc, BrowserChallengeError):
            public_error = 'Сайт використовує додатковий захист. Спробуйте ще раз за кілька секунд.'
            detail = json.dumps({
                'type': 'browser_challenge',
                'url': exc.url,
                'returncode': exc.returncode,
                'challenge_detected': exc.challenge_detected,
                'stdout_length': exc.stdout_length,
                'diagnostics': exc.diagnostics,
            }, ensure_ascii=False)
            error_type = 'browser_challenge'
        else:
            detail = str(exc).strip()
            lowered = detail.lower()
            if any(token in lowered for token in ('curl:', 'timeout', 'timed out', 'failed to connect')):
                public_error = 'Сайт тимчасово не відповідає. Повторіть пошук за кілька секунд.'
                error_type = 'network'
            elif any(token in detail for token in ('Read-only file system', 'Permission denied')) or detail.startswith('[Errno'):
                public_error = 'Не вдалося підготувати афішу. Повторіть пошук.'
                error_type = 'local_io'
            elif (
                'AniTube не повернув' in detail
                or str(job.get('site_host') or '').lower() in {'anitube.in.ua', 'www.anitube.in.ua'}
            ):
                public_error = 'AniTube не повернув доступних серій або відеопотоків. Захист сайту заблокував завантаження плейлиста.'
                error_type = 'no_sources'
            else:
                public_error = detail or 'Не вдалося знайти відео.'
                error_type = 'generic'
        job.update({
            'status': 'error',
            'error': public_error,
            'error_detail': detail[-1800:],
            'error_type': error_type,
            'resolved_at': int(time.time()),
        })
    _save(job)

def _template(name: str, **replace: str) -> str:
    text = (BASE / name).read_text(encoding='utf-8')
    for key, value in replace.items():
        text = text.replace(key, value)
    return text


def _volume_status() -> dict:
    process = subprocess.run(
        [str(VOLUME_POLICY), 'status'], text=True, capture_output=True,
        timeout=10, check=False,
    )
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout or 'Не вдалося прочитати гучність.').strip()[-500:])
    data = json.loads(process.stdout)
    try:
        saved = json.loads(VOLUME_STATE.read_text(encoding='utf-8'))
        data['last_nonzero'] = int(saved.get('last_nonzero') or 25)
    except Exception:
        data['last_nonzero'] = int(data.get('master') or 25) or 60
    return data


def _set_volume(level: int) -> dict:
    payload = json.dumps({'level': level}).encode('utf-8')
    request_object = Request(
        'http://127.0.0.1:8101/api/volume', data=payload,
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    try:
        with urlopen(request_object, timeout=4) as response:
            data = json.loads(response.read().decode('utf-8'))
    except Exception as exc:
        raise RuntimeError(f'Ізольований контролер гучності недоступний: {exc}') from exc
    if not isinstance(data, dict) or data.get('error'):
        raise RuntimeError(str((data or {}).get('error') or 'Не вдалося змінити гучність.'))
    return data


def _hyperion_status() -> dict:
    process = subprocess.run(
        [str(HOME_EDGE_CONTROL), 'status'], text=True, capture_output=True,
        timeout=10, check=False,
    )
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout or 'Не вдалося прочитати стан Hyperion.').strip()[-500:])
    match = re.search(r'^hyperion=live=(true|false)\s+source=(.*)$', process.stdout, re.M)
    if not match:
        raise RuntimeError('Home Edge не повернув стан Hyperion.')
    enabled = match.group(1) == 'true'
    return {'enabled': enabled, 'live': enabled, 'source': match.group(2).strip()}


def _game_status() -> dict:
    mode = player.mode_status()
    state: dict = {}
    try:
        state = json.loads(GAME_STATE.read_text(encoding='utf-8'))
    except Exception:
        state = {}
    process = subprocess.run(
        ['/usr/bin/pgrep', '-f', '^/usr/bin/(fuse|fuse-sdl)( |$)'],
        text=True, capture_output=True, timeout=3, check=False,
    )
    active = mode.get('mode') == 'games'
    title = str(state.get('title') or '') if active else ''
    control_profile = 'universal'
    return {
        'active': active,
        'mode': mode.get('mode'),
        'tv_mode': mode.get('tv_mode'),
        'view': state.get('view') if active else None,
        'title': title,
        'selected': state.get('selected') if active else None,
        'count': state.get('count') if active else None,
        'control_profile': control_profile if active else None,
        'fuse_running': process.returncode == 0 if active else False,
        'updated_at': state.get('updated_at') if active else None,
    }


def _game_input(action: str, phase: str) -> dict:
    if action == 'android':
        return player.switch_mode('android')
    if action == 'library':
        action, phase = 'f10', 'tap'
    allowed = {'up', 'down', 'left', 'right', 'a', 'b', 'x', 'y', 'l', 'r', 'start', 'select', 'enter', 'space', 'f10', 'escape', 'backspace'}
    if action not in allowed or phase not in {'down', 'up', 'tap'}:
        raise ValueError('Невідома команда геймпада.')
    current = _game_status()
    if not current.get('active'):
        raise RuntimeError('Ігровий режим зараз не активний.')
    if GAME_INPUT_SOCKET.exists():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(str(GAME_INPUT_SOCKET))
                client.sendall((json.dumps({'action': action, 'phase': phase}) + '\n').encode())
                response = json.loads(client.recv(4096) or b'{}')
            if not response.get('ok'):
                raise RuntimeError(str(response.get('error') or 'Input broker відхилив кнопку.'))
            return _game_status()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    unit = f'home-edge-game-{phase}@{action}.service'
    process = subprocess.run([SYSTEMCTL, '--user', 'start', '--wait', unit], text=True, capture_output=True, timeout=10, check=False)
    if process.returncode:
        subprocess.run([SYSTEMCTL, '--user', 'reset-failed', unit], text=True, capture_output=True, timeout=5, check=False)
        raise RuntimeError((process.stderr or process.stdout or 'Не вдалося передати кнопку.').strip()[-500:])
    return _game_status()


def _supported_remote_actions(active: str) -> list[str]:
    actions = {
        'android': {'up','down','left','right','ok','back','home','menu','playpause','rewind','forward','stop'},
        'youtube': {'up','down','left','right','ok','back','home','menu','playpause','rewind','forward','stop'},
        'airscreen': {'up','down','left','right','ok','back','home','menu','playpause','rewind','forward','stop'},
        'chrome': {'up','down','left','right','ok','back','menu','playpause','rewind','forward','fullscreen','mute','reload'},
        'kiosk': {'up','down','left','right','ok','back','menu','playpause','rewind','forward','fullscreen','mute','reload'},
        'mpv': {'playpause','rewind','forward','stop','fullscreen','subtitles'},
        'vlc': {'up','down','left','right','ok','back','playpause','rewind','forward','stop','fullscreen','mute'},
        'games': {'up','down','left','right','a','b','x','y','l','r','start','select','library','android'},
    }
    return sorted(actions.get(active, set()))


def _browser_keyboard_status() -> tuple[bool, str | None]:
    try:
        process = subprocess.run(
            [str(CHROME_MEDIA), 'focus'], text=True, capture_output=True,
            timeout=3, check=False,
        )
        if process.returncode:
            return False, None
        data = json.loads(process.stdout or '{}')
        shown = bool(data.get('needs_keyboard'))
        raw = str(data.get('context') or '')
        context = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20] if shown and raw else None
        return shown, context
    except Exception:
        return False, None


def _android_keyboard_status() -> tuple[bool, str | None]:
    now = time.monotonic()
    with _REMOTE_IME_LOCK:
        cached_at = float(_REMOTE_IME_CACHE.get('at') or 0.0)
        if now - cached_at < 1.5:
            return bool(_REMOTE_IME_CACHE.get('shown')), _REMOTE_IME_CACHE.get('context') if isinstance(_REMOTE_IME_CACHE.get('context'), str) else None
        try:
            process = subprocess.run(
                ['/usr/bin/adb', '-s', ANDROID_SERIAL, 'shell', 'dumpsys', 'input_method'],
                text=True, capture_output=True, timeout=3, check=False,
            )
            output = process.stdout if process.returncode == 0 else ''
            shown = any(token in output for token in ('mInputShown=true', 'mImeWindowVis=0x1', 'mImeWindowVis=0x3'))
            served = next((line.strip() for line in output.splitlines() if 'mServedView=' in line or 'mCurFocusedWindow=' in line), '')
            context = hashlib.sha256(served.encode('utf-8')).hexdigest()[:20] if shown and served else ('android-ime' if shown else None)
        except Exception:
            shown, context = False, None
        _REMOTE_IME_CACHE.update({'at': now, 'shown': shown, 'context': context})
        return shown, context


def _remote_status_base(mode: dict) -> dict:
    active=str(mode.get('mode') or 'unknown')
    if active in {'android','youtube','airscreen'}:profile='android'
    elif active in {'mpv','vlc'}:profile='player'
    elif active in {'chrome','kiosk'}:profile='browser'
    elif active=='games':profile='game'
    elif active=='off':profile='off'
    else:profile='unknown'
    labels={'android':'Android TV','youtube':'YouTube TV','airscreen':'AirScreen','mpv':'MPV','vlc':'VLC','chrome':'Chrome','kiosk':'YouTube Web','games':'Ігри','off':'Вимкнено','unknown':'Невідомий режим'}
    pointer_actions=[];keyboard_layouts=[]
    if profile=='browser':pointer_actions=['move','tap','drag_start','drag_end','scroll'];keyboard_layouts=['en','uk','ru']
    elif profile=='android':pointer_actions=['move','tap','scroll'];keyboard_layouts=['en']
    elif active=='vlc':pointer_actions=['move','tap','drag_start','drag_end','scroll']
    return {**mode,'profile':profile,'label':labels.get(active,active),'supports_volume':profile not in {'off','unknown'},'supported_actions':_supported_remote_actions(active),'pointer_supported':bool(pointer_actions),'pointer_actions':pointer_actions,'keyboard_supported':bool(keyboard_layouts),'keyboard_layouts':keyboard_layouts,'needs_keyboard':False,'keyboard_context_id':None}


def _remote_status_uncached(mode: dict | None=None) -> dict:
    result=_remote_status_base(mode or player.mode_status());profile=str(result.get('profile') or 'unknown')
    if profile=='browser':
        result['needs_keyboard'],result['keyboard_context_id']=_browser_keyboard_status()
    elif profile=='android':
        result['needs_keyboard'],result['keyboard_context_id']=_android_keyboard_status()
    if profile in {'player','browser'}:result['player']=player.status()
    elif profile=='game':result['game']=_game_status()
    return result


def _refresh_remote_status_cache(mode: dict) -> None:
    try:data=_remote_status_uncached(mode)
    except Exception:data=_remote_status_base(mode)
    with _REMOTE_STATUS_CACHE_LOCK:
        _REMOTE_STATUS_CACHE.update({'at':time.monotonic(),'mode':str(mode.get('mode') or 'unknown'),'data':data,'refreshing':False})


def _remote_status() -> dict:
    mode=player.mode_status();active=str(mode.get('mode') or 'unknown');now=time.monotonic()
    with _REMOTE_STATUS_CACHE_LOCK:
        cached=_REMOTE_STATUS_CACHE.get('data');same=_REMOTE_STATUS_CACHE.get('mode')==active
        stale=not same or now-float(_REMOTE_STATUS_CACHE.get('at') or 0.0)>3.0
        if stale and not _REMOTE_STATUS_CACHE.get('refreshing'):
            _REMOTE_STATUS_CACHE['refreshing']=True
            threading.Thread(target=_refresh_remote_status_cache,args=(dict(mode),),name='remote-status-refresh',daemon=True).start()
        if same and isinstance(cached,dict):
            return {**cached,**mode,'status_cached':True}
    return {**_remote_status_base(mode),'status_cached':False}


def _run_adb_key(keycode: str) -> None:
    process = subprocess.run(
        ['/usr/bin/adb', '-s', ANDROID_SERIAL, 'shell', 'input', 'keyevent', keycode],
        text=True, capture_output=True, timeout=8, check=False,
    )
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout or 'Android TV не прийняв кнопку.').strip()[-500:])


def _desktop_target(mode: str) -> tuple[dict[str, str], str]:
    now = time.monotonic()
    with _DESKTOP_TARGET_LOCK:
        if (_DESKTOP_TARGET_CACHE.get('mode') == mode and now - float(_DESKTOP_TARGET_CACHE.get('at') or 0.0) < 20.0
                and isinstance(_DESKTOP_TARGET_CACHE.get('env'), dict) and str(_DESKTOP_TARGET_CACHE.get('window') or '').isdigit()):
            return dict(_DESKTOP_TARGET_CACHE['env']), str(_DESKTOP_TARGET_CACHE['window'])
    auth_files = sorted(Path('/run/user/1000').glob('.mutter-Xwaylandauth.*'),key=lambda item:item.stat().st_mtime,reverse=True)
    if not auth_files:raise RuntimeError('Xwayland authorization не знайдено.')
    pattern='Google Chrome' if mode in {'chrome','kiosk'} else ('VLC' if mode=='vlc' else '.*')
    for auth in auth_files:
        for display in (':0',':1',':2'):
            env=os.environ.copy();env.update({'HOME':str(HOME),'DISPLAY':display,'XAUTHORITY':str(auth),'XDG_RUNTIME_DIR':'/run/user/1000','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1000/bus'})
            probe=subprocess.run([XDOTOOL,'search','--onlyvisible','--name',pattern],text=True,capture_output=True,timeout=1,check=False,env=env)
            windows=[line.strip() for line in probe.stdout.splitlines() if line.strip().isdigit()]
            if probe.returncode==0 and windows:
                window=windows[-1]
                with _DESKTOP_TARGET_LOCK:_DESKTOP_TARGET_CACHE.update({'at':now,'mode':mode,'env':env,'window':window})
                return env,window
    raise RuntimeError('Активне Xwayland-вікно для поточного режиму не знайдено.')


def _run_xdotool(mode: str, arguments: list[str], timeout: float = 2.0) -> None:
    env,window=_desktop_target(mode)
    focused=subprocess.run([XDOTOOL,'getwindowfocus'],text=True,capture_output=True,timeout=1,check=False,env=env).stdout.strip()
    if focused!=window:
        focus=subprocess.run([XDOTOOL,'windowactivate',window],text=True,capture_output=True,timeout=1,check=False,env=env)
        if focus.returncode:raise RuntimeError((focus.stderr or focus.stdout or 'Не вдалося активувати desktop-вікно.').strip()[-500:])
    process=subprocess.run([XDOTOOL,*arguments],text=True,capture_output=True,timeout=timeout,check=False,env=env)
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout or 'X11-команду не застосовано.').strip()[-500:])


def _run_desktop_key(mode: str, action: str, phase: str) -> None:
    if phase != 'tap':
        raise ValueError('Desktop-пульт підтримує коротке натискання.')
    keymap = {
        'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
        'enter': 'Return', 'escape': 'Escape', 'tab': 'Tab', 'backspace': 'BackSpace',
        'space': 'space', 'playpause': 'space', 'fullscreen': 'f', 'mute': 'm',
        'stop': 's', 'reload': 'F5',
    }
    if action == 'back':
        key = 'alt+Left' if mode in {'chrome', 'kiosk'} else 'BackSpace'
    elif action == 'rewind':
        key = 'Left' if mode == 'vlc' else 'j'
    elif action == 'forward':
        key = 'Right' if mode == 'vlc' else 'l'
    else:
        key = keymap.get(action)
    if not key:
        raise ValueError('Невідома desktop-кнопка.')
    _run_xdotool(mode, ['key', '--clearmodifiers', key])


def _pointer_input(action: str, dx: float = 0.0, dy: float = 0.0) -> None:
    allowed = {'move', 'tap', 'down', 'up', 'scroll', 'status'}
    if action not in allowed:
        raise ValueError('Невідома команда фізичного курсора.')
    if action == 'move':
        command = f"move {max(-120, min(120, int(round(dx))))} {max(-120, min(120, int(round(dy))))}"
    elif action == 'scroll':
        command = f"scroll {max(-6, min(6, int(round(dy))))}"
    else:
        command = action
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.7)
            client.connect(str(POINTER_INPUT_SOCKET))
            client.sendall((command + '\n').encode())
            response = client.recv(128).decode(errors='replace').strip()
    except OSError as exc:
        raise RuntimeError('Фізичний pointer broker недоступний.') from exc
    if response != 'ok':
        raise RuntimeError(response or 'Фізичний pointer broker відхилив команду.')


def _cancel_drag_timer() -> None:
    global _REMOTE_DRAG_TIMER
    if _REMOTE_DRAG_TIMER is not None:
        _REMOTE_DRAG_TIMER.cancel()
        _REMOTE_DRAG_TIMER = None


def _force_desktop_drag_release() -> None:
    global _REMOTE_DRAG_MODE, _REMOTE_DRAG_TIMER
    with _REMOTE_POINTER_LOCK:
        mode = _REMOTE_DRAG_MODE
        _REMOTE_DRAG_MODE = None
        _REMOTE_DRAG_TIMER = None
    if mode:
        try:
            _pointer_input('up')
        except Exception:
            pass


def _arm_drag_timer(mode: str) -> None:
    global _REMOTE_DRAG_TIMER, _REMOTE_DRAG_MODE
    _cancel_drag_timer()
    _REMOTE_DRAG_MODE = mode
    timer = threading.Timer(2.0, _force_desktop_drag_release)
    timer.daemon = True
    _REMOTE_DRAG_TIMER = timer
    timer.start()


def _run_desktop_pointer(mode: str, action: str, dx: float, dy: float) -> None:
    global _REMOTE_DRAG_MODE
    with _REMOTE_POINTER_LOCK:
        if action == 'move':
            _pointer_input('move', dx, dy)
            if _REMOTE_DRAG_MODE:
                _arm_drag_timer(mode)
        elif action == 'tap':
            _pointer_input('tap')
        elif action == 'scroll':
            repeat = max(1, min(6, int(abs(dy) // 18) + 1))
            _pointer_input('scroll', dy=(repeat if dy < 0 else -repeat))
        elif action == 'drag_start':
            if _REMOTE_DRAG_MODE:
                _pointer_input('up')
            _pointer_input('down')
            _arm_drag_timer(mode)
        elif action == 'drag_end':
            _cancel_drag_timer()
            _pointer_input('up')
            _REMOTE_DRAG_MODE = None
        else:
            raise ValueError('Невідома desktop-дія тачпада.')


def _adb_run(arguments: list[str], timeout: float = 8.0) -> str:
    process = subprocess.run(
        ['/usr/bin/adb', '-s', ANDROID_SERIAL, 'shell', *arguments],
        text=True, capture_output=True, timeout=timeout, check=False,
    )
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout or 'Android не застосував команду.').strip()[-500:])
    return process.stdout


def _android_display_size() -> tuple[int, int]:
    now = time.monotonic()
    cached = _ANDROID_SIZE_CACHE.get('size')
    if now - float(_ANDROID_SIZE_CACHE.get('at') or 0.0) < 15 and isinstance(cached, tuple):
        return int(cached[0]), int(cached[1])
    output = _adb_run(['wm', 'size'], timeout=4)
    match = re.search(r'(?:Physical|Override) size:\s*(\d+)x(\d+)', output)
    size = (int(match.group(1)), int(match.group(2))) if match else (1920, 1080)
    _ANDROID_SIZE_CACHE.update({'at': now, 'size': size})
    return size


def _run_android_pointer(action: str, dx: float, dy: float) -> None:
    width, height = _android_display_size()
    x1, y1 = _ANDROID_POINTER
    x2 = max(0, min(width - 1, int(round(x1 + dx * 2.2))))
    y2 = max(0, min(height - 1, int(round(y1 + dy * 2.2))))
    if action == 'move':
        _adb_run(['input', 'mouse', 'swipe', str(x1), str(y1), str(x2), str(y2), '45'])
        _ANDROID_POINTER[:] = [x2, y2]
    elif action == 'tap':
        _adb_run(['input', 'mouse', 'tap', str(x1), str(y1)])
    elif action == 'scroll':
        distance = max(120, min(int(height * 0.35), int(abs(dy) * 8)))
        end_y = max(0, min(height - 1, y1 - distance if dy > 0 else y1 + distance))
        _adb_run(['input', 'touchscreen', 'swipe', str(x1), str(y1), str(x1), str(end_y), '180'])
    else:
        raise ValueError('Ця Android-дія тачпада не підтримується.')


def _remote_pointer(action: str, dx: float, dy: float) -> dict:
    mode,profile,_mode_state=_fast_remote_context()
    pointer_actions={'browser':{'move','tap','drag_start','drag_end','scroll'},'android':{'move','tap','scroll'},'player':({'move','tap','drag_start','drag_end','scroll'} if mode=='vlc' else set())}.get(profile,set())
    if action not in pointer_actions:raise ValueError('Ця дія тачпада недоступна для поточного режиму.')
    if profile == 'browser' or mode == 'vlc':
        _run_desktop_pointer(mode, action, dx, dy)
    elif profile == 'android':
        _run_android_pointer(action, dx, dy)
    else:
        raise ValueError('Тачпад недоступний для поточного режиму.')
    return {'applied': True, 'mode': mode, 'action': action}


def _remote_keyboard(action: str, text: str, key: str, layout: str) -> dict:
    mode,profile,_mode_state=_fast_remote_context()
    layouts={'browser':{'en','uk','ru'},'android':{'en'}}.get(profile,set())
    if layout not in layouts:raise ValueError('Ця розкладка клавіатури недоступна для поточного режиму.')
    if profile == 'browser':
        if action == 'text':
            process = subprocess.run(
                [str(CHROME_MEDIA), 'text', text], text=True, capture_output=True,
                timeout=5, check=False,
            )
            if process.returncode:
                try:
                    detail = str(json.loads(process.stdout or '{}').get('error') or '')
                except Exception:
                    detail = ''
                raise RuntimeError(detail or (process.stderr or process.stdout or 'Текст не введено.').strip()[-500:])
        else:
            mapped = {'Enter': 'enter', 'Backspace': 'backspace', 'Escape': 'escape', 'Tab': 'tab', 'Space': 'space', 'ArrowUp': 'up', 'ArrowDown': 'down', 'ArrowLeft': 'left', 'ArrowRight': 'right'}[key]
            _run_desktop_key(mode, mapped, 'tap')
    elif profile == 'android':
        if action == 'text':
            if layout != 'en' or not text.isascii() or not re.fullmatch(r"[A-Za-z0-9 .,_@:/?+\-']+", text):
                raise ValueError('Android зараз підтримує безпечний латинський текст; українська й російська доступні у браузері.')
            _adb_run(['input', 'text', text.replace(' ', '%s')])
        else:
            code = {'Enter':'KEYCODE_ENTER','Backspace':'KEYCODE_DEL','Escape':'KEYCODE_ESCAPE','Tab':'KEYCODE_TAB','Space':'KEYCODE_SPACE','ArrowUp':'KEYCODE_DPAD_UP','ArrowDown':'KEYCODE_DPAD_DOWN','ArrowLeft':'KEYCODE_DPAD_LEFT','ArrowRight':'KEYCODE_DPAD_RIGHT'}[key]
            _run_adb_key(code)
    else:
        raise ValueError('Клавіатура недоступна для поточного режиму.')
    return {'applied': True, 'mode': mode, 'action': action}


def _fast_remote_context() -> tuple[str,str,dict]:
    mode=player.mode_status();active=str(mode.get('mode') or 'unknown')
    if active in {'android','youtube','airscreen'}:profile='android'
    elif active in {'mpv','vlc'}:profile='player'
    elif active in {'chrome','kiosk'}:profile='browser'
    elif active=='games':profile='game'
    elif active=='off':profile='off'
    else:profile='unknown'
    return active,profile,mode

def _remote_control(action: str, phase: str) -> dict:
    if phase not in {'tap', 'down', 'up'}:
        raise ValueError('Невідома фаза кнопки.')
    mode,profile,mode_state=_fast_remote_context()

    if profile == 'android':
        if phase != 'tap':
            raise ValueError('Android-пульт зараз підтримує коротке натискання.')
        keymap = {
            'up': 'KEYCODE_DPAD_UP', 'down': 'KEYCODE_DPAD_DOWN',
            'left': 'KEYCODE_DPAD_LEFT', 'right': 'KEYCODE_DPAD_RIGHT',
            'ok': 'KEYCODE_DPAD_CENTER', 'menu': 'KEYCODE_MENU',
            'playpause': 'KEYCODE_MEDIA_PLAY_PAUSE',
        }
        if action in keymap:
            _run_adb_key(keymap[action])
        elif action in {'back', 'home', 'rewind', 'forward', 'stop'}:
            command_name = {'forward': 'fast-forward'}.get(action, action)
            process = subprocess.run(
                [str(HOME_EDGE_CONTROL), command_name], text=True, capture_output=True,
                timeout=20, check=False,
            )
            if process.returncode:
                raise RuntimeError((process.stderr or process.stdout or 'Android-команду не виконано.').strip()[-500:])
        else:
            raise ValueError('Ця кнопка не підтримується Android-пультом.')

    elif profile == 'player':
        if phase != 'tap':
            raise ValueError('Пульт плеєра підтримує коротке натискання.')
        if mode == 'mpv':
            if action == 'playpause':
                player.control('toggle')
            elif action == 'rewind':
                player.control('back')
            elif action == 'forward':
                player.control('forward')
            elif action == 'stop':
                player.control('stop')
            elif action == 'fullscreen':
                result = player.command(['cycle', 'fullscreen'])
                if result.get('error') not in (None, 'success'):
                    raise RuntimeError(str(result.get('error')))
            elif action == 'subtitles':
                result = player.command(['cycle', 'sub'])
                if result.get('error') not in (None, 'success'):
                    raise RuntimeError(str(result.get('error')))
            else:
                raise ValueError('Ця кнопка не підтримується MPV-пультом.')
        else:
            mapping = {
                'playpause': 'playpause', 'rewind': 'rewind', 'forward': 'forward',
                'stop': 'stop', 'fullscreen': 'fullscreen', 'mute': 'mute',
                'up': 'up', 'down': 'down', 'left': 'left', 'right': 'right',
                'ok': 'enter', 'back': 'back',
            }
            target = mapping.get(action)
            if not target:
                raise ValueError('Ця кнопка не підтримується VLC-пультом.')
            _run_desktop_key(mode, target, 'tap')

    elif profile == 'browser':
        mapping = {
            'up': 'up', 'down': 'down', 'left': 'left', 'right': 'right',
            'ok': 'enter', 'back': 'back', 'menu': 'tab',
            'playpause': 'playpause', 'rewind': 'rewind', 'forward': 'forward',
            'fullscreen': 'fullscreen', 'mute': 'mute', 'reload': 'reload',
        }
        target = mapping.get(action)
        if not target:
            raise ValueError('Ця кнопка не підтримується браузерним пультом.')
        _run_desktop_key(mode, target, phase)

    elif profile == 'game':
        game_action = {'ok': 'a', 'back': 'b'}.get(action, action)
        return {**_game_input(game_action, phase), 'profile': 'game'}
    else:
        raise RuntimeError('Для поточного режиму немає активного пульта.')

    return {**mode_state,'profile':profile,'applied':True,'action':action}


def _set_hyperion(enabled: bool) -> dict:
    current = _hyperion_status()
    if current['enabled'] is enabled:
        return {**current, 'unchanged': True}
    command_name = 'hyperion-on' if enabled else 'hyperion-off'
    process = subprocess.run(
        [SYSTEMD_RUN, '--user', '--wait', '--collect', '--pipe', '--quiet',
         str(HOME_EDGE_CONTROL), command_name],
        text=True, capture_output=True, timeout=45, check=False,
    )
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout or 'Не вдалося змінити стан Hyperion.').strip()[-500:])
    deadline = time.time() + 8.0
    latest = _hyperion_status()
    while time.time() < deadline and latest['enabled'] is not enabled:
        time.sleep(0.25)
        latest = _hyperion_status()
    if latest['enabled'] is not enabled:
        raise RuntimeError('Команду Hyperion прийнято, але фізичний стан не підтверджено.')
    return {**latest, 'unchanged': False}


@app.after_request
def headers(response: Response) -> Response:
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/service-worker.js')
def service_worker() -> Response:
    response = send_from_directory(STATIC, 'service-worker.js', mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/'
    return response


@app.get('/posters/<path:filename>')
def poster_file(filename: str):
    _require()
    response = send_from_directory(POSTERS, filename)
    response.headers['Cache-Control'] = 'private, max-age=604800, immutable'
    return response


@app.get('/health')
def health() -> Response:
    return jsonify({'service': 'skeleton-cast', 'status': 'ok', 'player': player.status()})


@app.get('/')
@app.get('/phone')
def phone() -> str:
    _require()
    return _template('phone.html')


@app.get('/video')
def video() -> str:
    _require()
    return _template('video.html')


@app.get('/remote')
def remote() -> str:
    _require()
    return _template('remote.html')


@app.get('/tablet')
def tablet() -> str:
    _require()
    return _template('tablet.html')


@app.post('/api/tablet-capabilities')
def tablet_capabilities() -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    allowed = {
        'user_agent': str(data.get('user_agent') or '')[:500],
        'screen': str(data.get('screen') or '')[:80],
        'pixel_ratio': str(data.get('pixel_ratio') or '')[:40],
        'touch_points': str(data.get('touch_points') or '')[:40],
        'mp4_h264_aac': str(data.get('mp4_h264_aac') or '')[:40],
        'mp4': str(data.get('mp4') or '')[:40],
        'webm_vp8': str(data.get('webm_vp8') or '')[:40],
        'hls': str(data.get('hls') or '')[:40],
        'fullscreen': bool(data.get('fullscreen')),
        'client_ip': request.remote_addr or '',
        'observed_at': int(time.time()),
    }
    _atomic(STATE / 'samsung-tablet-capabilities.json', allowed)
    return jsonify({'status': 'ok', **allowed})


@app.get('/devices')
def devices() -> str:
    _require()
    return _template('devices.html')


@app.get('/install')
def install() -> str:
    _require()
    apk = STATIC / 'SkeletonTV.apk'
    size = f'{apk.stat().st_size // 1024} КБ' if apk.exists() else 'ще збирається'
    return _template('install.html', __APK_SIZE__=size)


@app.get('/download/SkeletonTV.apk')
def apk():
    _require()
    return send_from_directory(STATIC, 'SkeletonTV.apk', as_attachment=True, download_name='Home.apk')


@app.get('/api/native/app-update')
def native_app_update() -> Response:
    _require()
    try:
        return jsonify(native_app_update_manifest.build_update_manifest(STATIC, HOME_NATIVE_RELEASE))
    except native_app_update_manifest.NativeAppUpdateUnavailable:
        return jsonify({'error': 'Маніфест оновлення Home ще не готовий.'}), 503


@app.post('/api/share')
def share() -> Response:
    _require()
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        page_url, site_host, site_status = _page_url(str(data.get('url') or data.get('text') or ''))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    job_id = uuid.uuid4().hex[:16]
    job = {
        'job_id': job_id, 'status': 'resolving', 'page_url': page_url,
        'site_host': site_host, 'site_status': site_status,
        'title': 'Пошук потоків…', 'sources': [], 'created_at': int(time.time()),
    }
    _save(job)
    pool.submit(_resolve_job, job_id)
    return jsonify({'job_id': job_id, 'status': 'resolving', 'select_url': f'http://{LAN_HOST}:{PORT}/select/{job_id}'})


@app.get('/api/jobs/<job_id>')
def get_job(job_id: str) -> Response:
    _require()
    job = _load(job_id)
    safe = json.loads(json.dumps(job))
    for source in safe.get('sources', []):
        source.pop('url', None)
        source['headers'] = {
            'Referer': bool(source.get('headers', {}).get('Referer')),
            'User-Agent': bool(source.get('headers', {}).get('User-Agent')),
        }
    return jsonify(safe)


def _media_identity_from_payload(data: dict) -> media_search.MediaIdentity:
    original_title = str(data.get('original_title') or data.get('title') or data.get('query') or '').strip()
    if not original_title:
        raise ValueError('Вкажіть назву релізу.')
    return media_search.MediaIdentity(
        original_title=original_title,
        year=_optional_int(data.get('year')),
        season=_optional_int(data.get('season')),
        episode=_optional_int(data.get('episode')),
    )


def _optional_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _media_target_default() -> dict:
    return {
        'schema': 'skeleton.media.target.v1',
        'target': 'tv',
        'label': TARGET_LABELS['tv'],
        'updated_at': int(time.time()),
        'revision': 0,
        'device_id': None,
        'charged': False,
        'charge': None,
    }


def _media_target_state() -> dict:
    state = _read_json(MEDIA_TARGET_STATE)
    target = str(state.get('target') or 'tv').strip().lower()
    labels = _endpoint_labels()
    if target not in labels:
        target = 'tv'
    charge = state.get('charge') if isinstance(state.get('charge'), dict) else None
    return {
        **_media_target_default(),
        **state,
        'target': target,
        'label': labels[target],
        'device_id': _endpoint_adapters()[target].device_id,
        'charged': bool(charge),
        'charge': charge,
    }


def _set_media_target(target: str, *, reason: str, observed: dict | None = None, charge: dict | object = None) -> dict:
    previous = _media_target_state()
    revision = int(previous.get('revision') or 0) + (0 if previous.get('target') == target else 1)
    adapter = _endpoint_adapters()[target]
    state = {
        'schema': 'skeleton.media.target.v1',
        'target': target,
        'label': adapter.label,
        'device_id': adapter.device_id,
        'revision': revision,
        'updated_at': int(time.time()),
        'reason': reason,
    }
    if charge is None:
        existing = previous.get('charge') if isinstance(previous.get('charge'), dict) else None
        if existing:
            state['charge'] = existing
            state['charged'] = True
    elif isinstance(charge, dict):
        state['charge'] = charge
        state['charged'] = True
    elif charge is _CLEAR_CHARGE:
        state['charge'] = None
        state['charged'] = False
    if isinstance(observed, dict):
        state['observed'] = observed
    _atomic(MEDIA_TARGET_STATE, state)
    return state


def _find_job_source(job_id: object, source_id: object) -> tuple[dict, dict]:
    job = _load(str(job_id or ''))
    source = next((item for item in job.get('sources', []) if str(item.get('source_id') or '') == str(source_id or '')), None)
    if not isinstance(source, dict):
        raise RuntimeError('Поточний потік більше не знайдено у завданні.')
    return job, source


def _playback_position(status: dict) -> float:
    for key in ('time-pos', 'time_pos', 'position_seconds', 'position'):
        value = status.get(key)
        if isinstance(value, (int, float)):
            return max(0.0, float(value))
    return 0.0


def _playback_paused(status: dict) -> bool:
    if isinstance(status.get('pause'), bool):
        return bool(status.get('pause'))
    if str(status.get('mode') or '').strip().lower() in {'video', 'tv', 'youtube'} and isinstance(status.get('playing'), bool):
        return not bool(status.get('playing'))
    state = str(status.get('playback_state') or status.get('playback_status') or '').strip().lower()
    return state in {'paused', 'pause'}


def _playback_playing(status: dict) -> bool:
    if 'playing' in status:
        return bool(status.get('playing'))
    if isinstance(status.get('pause'), bool):
        return not bool(status.get('pause'))
    state = str(status.get('playback_state') or status.get('playback_status') or '').strip().lower()
    return state in {'playing', 'play'}


def _youtube_video_id(value: object) -> str | None:
    text = str(value or '').strip()
    if not text:
        return None
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', text):
        return text
    try:
        parsed = urlparse(text)
    except Exception:
        return None
    host = parsed.netloc.lower()
    if host.endswith('youtu.be'):
        candidate = parsed.path.strip('/').split('/')[0]
        return candidate if re.fullmatch(r'[A-Za-z0-9_-]{11}', candidate or '') else None
    if 'youtube.com' in host or 'youtube-nocookie.com' in host:
        fragment = urlparse(parsed.fragment)
        query_id = (parse_qs(parsed.query).get('v') or parse_qs(parsed.fragment).get('v') or parse_qs(fragment.query).get('v') or [''])[0]
        if re.fullmatch(r'[A-Za-z0-9_-]{11}', query_id or ''):
            return query_id
        parts = [part for part in parsed.path.split('/') if part]
        for marker in ('embed', 'shorts', 'live', 'watch'):
            if marker in parts:
                index = parts.index(marker) + 1
                if index < len(parts) and re.fullmatch(r'[A-Za-z0-9_-]{11}', parts[index]):
                    return parts[index]
        fragment_path = fragment.path
        fragment_parts = [part for part in fragment_path.split('/') if part]
        if 'watch' in fragment_parts:
            index = fragment_parts.index('watch') + 1
            if index < len(fragment_parts) and re.fullmatch(r'[A-Za-z0-9_-]{11}', fragment_parts[index]):
                return fragment_parts[index]
    return None


def _source_video_id(source: dict) -> str | None:
    for key in ('video_id', 'youtube_id', 'youtube_video_id'):
        found = _youtube_video_id(source.get(key))
        if found:
            return found
    return _youtube_video_id(source.get('url'))


def _youtube_watch_url(video_id: str, position: float = 0.0) -> str:
    query = {'v': video_id}
    seconds = int(max(0.0, float(position)))
    if seconds > 0:
        query['t'] = f'{seconds}s'
    return urlunparse(('https', 'www.youtube.com', '/watch', '', urlencode(query), ''))


def _samsung_source_payload(source: dict, position: float) -> tuple[str, str, str | None]:
    video_id = _source_video_id(source)
    kind = str(source.get('kind') or '').strip().lower()
    if video_id and (kind == 'youtube' or 'youtube' in str(source.get('url') or '').lower()):
        return 'youtube', _youtube_watch_url(video_id, position), video_id
    return 'video', str(source.get('url') or ''), video_id


def _same_source_episode(left: dict, right: dict) -> bool:
    left_season = str(left.get('season') or '').strip()
    right_season = str(right.get('season') or '').strip()
    left_episode = str(left.get('episode') or '').strip()
    right_episode = str(right.get('episode') or '').strip()
    if left_season or right_season or left_episode or right_episode:
        return left_season == right_season and left_episode == right_episode
    return True


def _same_source_voice(left: dict, right: dict) -> bool:
    keys = ('translation', 'group', 'audio_language')
    return all(str(left.get(key) or '').strip().casefold() == str(right.get(key) or '').strip().casefold() for key in keys if left.get(key) or right.get(key))


def _source_height(source: dict) -> int:
    try:
        return int(source.get('height') or re.search(r'(\d{3,4})', str(source.get('quality') or '')).group(1))
    except Exception:
        return 0


def _codec_compatible(source: dict) -> bool:
    video = str(source.get('video_codec') or source.get('vcodec') or source.get('codec') or '').lower()
    audio = str(source.get('audio_codec') or source.get('acodec') or '').lower()
    video_ok = not video or any(token in video for token in ('h264', 'avc'))
    audio_ok = not audio or 'aac' in audio
    return video_ok and audio_ok


def _samsung_capabilities() -> dict:
    return _read_json(STATE / 'samsung-tablet-capabilities.json')


def _samsung_max_height() -> int:
    caps = _samsung_capabilities()
    try:
        explicit = int(caps.get('max_height') or 0)
    except (TypeError, ValueError):
        explicit = 0
    connection = str(caps.get('connection') or caps.get('network') or caps.get('network_type') or '').strip().lower()
    if connection in {'wifi', 'wi-fi', 'wireless'}:
        try:
            return min(720, int(caps.get('wifi_max_height') or explicit or 720))
        except (TypeError, ValueError):
            return 720
    return min(720, explicit) if explicit > 0 else 720


def _adapt_source_for_samsung(job: dict, current_source: dict) -> dict:
    max_height = _samsung_max_height()
    current_is_youtube = str(current_source.get('kind') or '').lower() == 'youtube'
    candidates = [
        item for item in job.get('sources', [])
        if isinstance(item, dict)
        and _same_source_episode(item, current_source)
        and _same_source_voice(item, current_source)
        and _source_height(item) <= max_height
        and _codec_compatible(item)
        and (current_is_youtube or str(item.get('kind') or '').lower() != 'youtube')
    ]
    if not candidates:
        raise RuntimeError('Для Samsung не знайдено сумісного потоку цієї серії та озвучки.')
    current_id = str(current_source.get('source_id') or '')
    candidates.sort(key=lambda item: (
        0 if str(item.get('source_id') or '') == current_id else 1,
        -_source_height(item),
        0 if _same_source_voice(item, current_source) else 1,
        int(item.get('order') or 0),
    ))
    return candidates[0]


def _next_samsung_revision() -> int:
    desired = _read_json(SAMSUNG_RECEIVER_DESIRED)
    status = _read_json(SAMSUNG_RECEIVER_STATUS)
    return max(int(desired.get('revision') or 0), int(status.get('revision') or 0)) + 1


def _samsung_action_id() -> str:
    return uuid.uuid4().hex


def _write_samsung_desired(job: dict, source: dict, *, position: float) -> dict:
    revision = _next_samsung_revision()
    mode, url, video_id = _samsung_source_payload(source, position)
    desired = {
        'schema': 'skeleton.samsung.media_desired.v1',
        'revision': revision,
        'mode': mode,
        'url': url,
        'position_seconds': round(max(0.0, float(position)), 3),
        'action': 'play',
        'action_id': _samsung_action_id(),
        'updated_at': int(time.time()),
        'job_id': job.get('job_id'),
        'source_id': source.get('source_id'),
        'display_title': job.get('title') or source.get('title') or source.get('page_title'),
        'poster': job.get('poster') or source.get('poster'),
        'episode': source.get('episode'),
        'season': source.get('season'),
        'quality': source.get('quality'),
        'translation': source.get('translation'),
        'media_title': job.get('title') or source.get('title') or source.get('page_title'),
        'channel_id': source.get('channel_id'),
        'channel_number': source.get('channel_number'),
        'picon': source.get('picon'),
    }
    if video_id:
        desired['video_id'] = video_id
    _atomic(SAMSUNG_RECEIVER_DESIRED, desired)
    return desired


def _write_samsung_action(action: str) -> dict:
    desired = _read_json(SAMSUNG_RECEIVER_DESIRED)
    if not desired:
        raise RuntimeError('Samsung не має активного бажаного стану для керування.')
    desired.update({
        'action': action,
        'action_id': _samsung_action_id(),
        'updated_at': int(time.time()),
    })
    _atomic(SAMSUNG_RECEIVER_DESIRED, desired)
    return desired


def _receiver_media(status: dict) -> dict:
    media = status.get('media')
    return media if isinstance(media, dict) else status


def _samsung_receiver_status() -> dict:
    status = _read_json(SAMSUNG_RECEIVER_STATUS)
    if str(status.get('device_id') or SAMSUNG_DEVICE_ID) != SAMSUNG_DEVICE_ID:
        return {}
    return status


def _destination_verified(status: dict, *, revision: int, source_id: object, position: float, paused: bool) -> bool:
    if int(status.get('revision') or 0) < int(revision):
        return False
    media = _receiver_media(status)
    status_source_id = media.get('source_id') or _read_json(SAMSUNG_RECEIVER_DESIRED).get('source_id')
    if str(status_source_id or '') != str(source_id or ''):
        return False
    if 'playing' not in media or not any(key in media for key in ('position_seconds', 'position', 'time-pos', 'time_pos')):
        return False
    if abs(_playback_position(media) - float(position)) > MEDIA_TARGET_POSITION_TOLERANCE_SECONDS:
        return False
    if paused:
        return _playback_paused(media) and not _playback_playing(media)
    return _playback_playing(media) and not _playback_paused(media)


def _receiver_acknowledged(status: dict, desired: dict) -> bool:
    if int(status.get('revision') or 0) < int(desired.get('revision') or 0):
        return False
    expected_mode = str(desired.get('mode') or '').strip().lower()
    observed_mode = str(status.get('mode') or _receiver_media(status).get('mode') or '').strip().lower()
    return not expected_mode or observed_mode == expected_mode


def _wait_for_samsung_postcondition(desired: dict) -> dict:
    deadline = time.monotonic() + MEDIA_TARGET_WAIT_SECONDS
    last = {}
    while time.monotonic() <= deadline:
        last = _samsung_receiver_status()
        if str(desired.get('mode') or '').strip().lower() == 'youtube' and _receiver_acknowledged(last, desired):
            return last
        if _destination_verified(
            last,
            revision=int(desired.get('revision') or 0),
            source_id=desired.get('source_id'),
            position=float(desired.get('position_seconds') or 0.0),
            paused=str(desired.get('action') or '').lower() == 'pause',
        ):
            return last
        time.sleep(0.1)
    raise RuntimeError(f'Samsung не підтвердив цільовий стан: revision={desired.get("revision")} status={last}')


def _wait_for_tv_postcondition(*, source_id: object, position: float, paused: bool, video_id: object = None, url: object = None) -> dict:
    deadline = time.monotonic() + MEDIA_TARGET_WAIT_SECONDS
    last = {}
    expected_video_id = _youtube_video_id(video_id) or _youtube_video_id(url)
    while time.monotonic() <= deadline:
        last = player.status()
        observed_video_id = _youtube_video_id(last.get('video_id')) or _youtube_video_id(last.get('url'))
        identity_matches = (
            str(last.get('source_id') or '') == str(source_id or '')
            or (expected_video_id and observed_video_id == expected_video_id)
        )
        if identity_matches and abs(_playback_position(last) - float(position)) <= MEDIA_TARGET_POSITION_TOLERANCE_SECONDS:
            if paused and _playback_paused(last) and not _playback_playing(last):
                return last
            if not paused and _playback_playing(last) and not _playback_paused(last):
                return last
        time.sleep(0.1)
    raise RuntimeError(f'TV не підтвердив цільовий стан: source_id={source_id} status={last}')


def _tv_session_snapshot() -> tuple[dict, dict, dict]:
    status = player.status()
    if not status.get('running') or not status.get('job_id') or not status.get('source_id'):
        raise RuntimeError('На TV немає відновлюваної медіасесії.')
    job, source = _find_job_source(status.get('job_id'), status.get('source_id'))
    return status, job, source


def _samsung_session_snapshot() -> tuple[dict, dict, dict]:
    status = _samsung_receiver_status()
    media = _receiver_media(status)
    desired = _read_json(SAMSUNG_RECEIVER_DESIRED)
    job_id = media.get('job_id') or desired.get('job_id')
    source_id = media.get('source_id') or desired.get('source_id')
    if not job_id or not source_id:
        raise RuntimeError('Samsung не повідомив відновлювану медіасесію.')
    job, source = _find_job_source(job_id, source_id)
    return status, job, source


def _capture_media_for_endpoint(endpoint_id: str, status: dict) -> dict:
    media = dict(_receiver_media(status))
    if endpoint_id == 'samsung':
        desired = _read_json(SAMSUNG_RECEIVER_DESIRED)
        if not any(key in media for key in ('position_seconds', 'position', 'time-pos', 'time_pos')) and 'position_seconds' in desired:
            media['position_seconds'] = desired.get('position_seconds')
        action = str(desired.get('action') or '').strip().lower()
        if 'playing' not in media and action in {'play', 'pause'}:
            media['playing'] = action == 'play'
        if 'pause' not in media and action in {'play', 'pause'}:
            media['pause'] = action == 'pause'
        video_id = (
            _youtube_video_id(media.get('video_id'))
            or _youtube_video_id(desired.get('video_id'))
            or _youtube_video_id(desired.get('url'))
        )
        if video_id:
            media['video_id'] = video_id
        if not media.get('url') and desired.get('url'):
            media['url'] = desired.get('url')
    return media


class MediaEndpointAdapter:
    endpoint_id = ''
    device_id = None
    label = ''
    backend = ''
    family = ''
    history_id = ''
    capabilities: tuple[str, ...] = ()

    def history_namespace(self) -> str:
        return _stable_history_id(self.history_id or self.endpoint_id or self.device_id)

    def resume_from_history(self, item: dict, job: dict, source: dict, position: float) -> dict:
        capture = {
            'job_id': job.get('job_id'),
            'source_id': source.get('source_id'),
            'position_seconds': max(0.0, float(position)),
            'paused': False,
            'was_playing': False,
        }
        return self.start_from_capture(capture)

    def descriptor(self) -> dict:
        return {
            'target': self.endpoint_id,
            'endpoint_id': self.endpoint_id,
            'device_id': self.device_id,
            'history_id': self.history_namespace(),
            'label': self.label,
            'backend': self.backend,
            'family': self.family,
            'capabilities': list(self.capabilities),
        }

    def status(self) -> dict:
        return {}

    def session_snapshot(self) -> tuple[dict, dict, dict]:
        raise RuntimeError(f'{self.label} не має відновлюваної медіасесії.')

    def adapt_source(self, job: dict, current_source: dict) -> dict:
        return current_source

    def start_from_capture(self, capture: dict) -> dict:
        raise RuntimeError(f'{self.label} не підтримує запуск із захоплення.')

    def verify_destination(self, capture: dict, started: dict) -> dict:
        raise RuntimeError(f'{self.label} не підтвердив цільовий стан.')

    def pause_source_after_move(self, capture: dict) -> dict:
        return {}


class TvEndpointAdapter(MediaEndpointAdapter):
    endpoint_id = 'tv'
    device_id = None
    history_id = HOME_EDGE_TV_HISTORY_ID
    label = TARGET_LABELS['tv']
    backend = 'mpv'
    family = 'tv'
    capabilities = ('capture', 'handoff', 'history')

    def status(self) -> dict:
        return player.status()

    def session_snapshot(self) -> tuple[dict, dict, dict]:
        return _tv_session_snapshot()

    def start_from_capture(self, capture: dict) -> dict:
        job, source = _find_job_source(capture.get('job_id'), capture.get('source_id'))
        player.play(job, source, 'off')
        player.seek_absolute(float(capture.get('position_seconds') or 0.0))
        player.control('pause' if capture.get('paused') else 'play')
        return {'source_id': source.get('source_id')}

    def verify_destination(self, capture: dict, started: dict) -> dict:
        return _wait_for_tv_postcondition(
            source_id=capture.get('source_id'),
            position=float(capture.get('position_seconds') or 0.0),
            paused=bool(capture.get('paused')),
            video_id=capture.get('video_id'),
            url=capture.get('url'),
        )

    def pause_source_after_move(self, capture: dict) -> dict:
        if capture.get('was_playing'):
            return player.control('pause')
        return {}


class SamsungEndpointAdapter(MediaEndpointAdapter):
    endpoint_id = 'samsung'
    device_id = SAMSUNG_DEVICE_ID
    history_id = SAMSUNG_DEVICE_ID
    label = TARGET_LABELS['samsung']
    backend = 'samsung-media-receiver'
    family = 'tablet_kiosk'
    capabilities = ('capture', 'handoff', 'history')

    def status(self) -> dict:
        return _samsung_receiver_status()

    def session_snapshot(self) -> tuple[dict, dict, dict]:
        return _samsung_session_snapshot()

    def adapt_source(self, job: dict, current_source: dict) -> dict:
        return _adapt_source_for_samsung(job, current_source)

    def start_from_capture(self, capture: dict) -> dict:
        job, current_source = _find_job_source(capture.get('job_id'), capture.get('source_id'))
        source = self.adapt_source(job, current_source)
        desired = _write_samsung_desired(job, source, position=float(capture.get('position_seconds') or 0.0))
        if capture.get('paused'):
            desired = _write_samsung_action('pause')
        return desired

    def verify_destination(self, capture: dict, started: dict) -> dict:
        return _wait_for_samsung_postcondition(started)

    def pause_source_after_move(self, capture: dict) -> dict:
        if capture.get('was_playing'):
            pause_desired = _write_samsung_action('pause')
            try:
                return _wait_for_samsung_postcondition(pause_desired)
            except RuntimeError:
                return {}
        return {}


_EXTRA_MEDIA_ENDPOINT_ADAPTERS: dict[str, MediaEndpointAdapter] = {}


def _endpoint_adapters() -> dict[str, MediaEndpointAdapter]:
    adapters: dict[str, MediaEndpointAdapter] = {
        'tv': TvEndpointAdapter(),
        'samsung': SamsungEndpointAdapter(),
    }
    adapters.update(_EXTRA_MEDIA_ENDPOINT_ADAPTERS)
    return adapters


def _endpoint_labels() -> dict[str, str]:
    return {endpoint_id: adapter.label for endpoint_id, adapter in _endpoint_adapters().items()}


def _endpoint_descriptors() -> list[dict]:
    return [adapter.descriptor() for adapter in _endpoint_adapters().values()]


def _capture_from_endpoint(endpoint_id: str, *, owner: str | None = None) -> dict:
    adapters = _endpoint_adapters()
    if endpoint_id not in adapters:
        raise ValueError('Невідомий медіа-екран.')
    adapter = adapters[endpoint_id]
    if 'capture' not in adapter.capabilities:
        raise RuntimeError(f'{adapter.label} не підтримує захоплення.')
    status, job, source = adapter.session_snapshot()
    media = _capture_media_for_endpoint(endpoint_id, status)
    capture = {
        'schema': 'skeleton.media.capture.v1',
        'owner': owner or MEDIA_CAPTURE_OWNER,
        'capture_id': uuid.uuid4().hex,
        'source_endpoint': endpoint_id,
        'job_id': job.get('job_id'),
        'source_id': source.get('source_id'),
        'url': media.get('url') or source.get('url'),
        'position_seconds': round(_playback_position(media), 3),
        'paused': _playback_paused(media),
        'was_playing': _playback_playing(media),
        'created_at': int(time.time()),
    }
    video_id = _youtube_video_id(media.get('video_id')) or _source_video_id(source) or _youtube_video_id(capture.get('url'))
    if video_id:
        capture['video_id'] = video_id
    return capture


def _charge_media_capture(endpoint_id: str | None = None) -> dict:
    state = _media_target_state()
    source_endpoint = str(endpoint_id or state.get('target') or 'tv').strip().lower()
    charge = _capture_from_endpoint(source_endpoint)
    return {**_set_media_target(str(state.get('target') or 'tv'), reason='capture_charged', charge=charge), 'unchanged': False}


def _handoff_capture_to_endpoint(target: str) -> dict:
    target = str(target or '').strip().lower()
    adapters = _endpoint_adapters()
    if target not in adapters:
        raise ValueError('Невідомий медіа-екран.')
    destination = adapters[target]
    if 'handoff' not in destination.capabilities:
        raise RuntimeError(f'{destination.label} не підтримує прийом захоплення.')
    state = _media_target_state()
    charge = state.get('charge') if isinstance(state.get('charge'), dict) else None
    if not charge:
        raise RuntimeError('Немає зарядженого медіа-захоплення.')
    source_endpoint = str(charge.get('source_endpoint') or '').strip().lower()
    if source_endpoint == target:
        return {
            **_set_media_target(target, reason='capture_released', charge=_CLEAR_CHARGE),
            'unchanged': False,
            'discharged': True,
            'returned_to_source': True,
        }
    source = adapters.get(source_endpoint)
    if not source:
        raise RuntimeError('Джерельний медіа-екран більше не доступний.')
    started = destination.start_from_capture(charge)
    observed = destination.verify_destination(charge, started)
    source.pause_source_after_move(charge)
    return {
        **_set_media_target(target, reason='handoff_verified', observed=observed, charge=_CLEAR_CHARGE),
        'unchanged': False,
        'desired': started,
    }


def _select_media_target(target: str) -> dict:
    target = str(target or '').strip().lower()
    if target not in _endpoint_adapters():
        raise ValueError('Невідомий медіа-екран.')
    current = _media_target_state()
    if current.get('target') == target:
        return {**current, 'unchanged': True}
    return {**_set_media_target(target, reason='selected'), 'unchanged': False}


def _switch_media_target(target: str) -> dict:
    target = str(target or '').strip().lower()
    adapters = _endpoint_adapters()
    if target not in adapters:
        raise ValueError('Невідомий медіа-екран.')
    current = _media_target_state()
    if current.get('target') == target:
        return {**current, 'unchanged': True}

    charge = _capture_from_endpoint(str(current.get('target') or 'tv'))
    started = adapters[target].start_from_capture(charge)
    observed = adapters[target].verify_destination(charge, started)
    adapters[str(current.get('target') or 'tv')].pause_source_after_move(charge)
    return {**_set_media_target(target, reason='handoff_verified', observed=observed), 'unchanged': False, 'desired': started}


@app.post('/api/media/search')
def media_source_search() -> Response:
    _require()
    data = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        identity = _media_identity_from_payload(data)
    except ValueError as exc:
        return jsonify({'status': 'error', 'message': str(exc), 'sources': []}), 400
    search_request = media_search.MediaSearchRequest(
        identity=identity,
        include_trailers=False,
        release_tracking=str(data.get('release_tracking') or '').lower() in {'1', 'true', 'yes'},
    )
    providers = media_search.providers_from_environment(resolve_page=resolve_page)
    result = media_search.search_media_sources(
        search_request,
        providers,
        circuit_breaker=_MEDIA_SEARCH_BREAKER,
        tracking_store=_MEDIA_RELEASE_TRACKING,
    )
    payload = media_search.public_result_payload(result)
    if result.playable_candidates:
        job_id = uuid.uuid4().hex[:16]
        job = {
            'job_id': job_id,
            'status': 'ready',
            'page_url': '',
            'site_host': 'media-search',
            'site_status': 'confirmed',
            'title': identity.release_query(),
            'sources': [media_search.source_to_job_source(candidate) for candidate in result.playable_candidates],
            'created_at': int(time.time()),
            'resolved_at': int(time.time()),
            'media_identity': {
                'original_title': identity.original_title,
                'year': identity.year,
                'season': identity.season,
                'episode': identity.episode,
            },
        }
        _save(job)
        payload['job_id'] = job_id
        payload['select_url'] = f'http://{LAN_HOST}:{PORT}/select/{job_id}'
    status_code = 200 if result.status in {'ready', 'empty'} else 503
    return jsonify(payload), status_code


@app.get('/api/media/target')
def media_target_get() -> Response:
    _require()
    state = _media_target_state()
    desired = _read_json(SAMSUNG_RECEIVER_DESIRED)
    receiver = _samsung_receiver_status()
    return jsonify({
        **state,
        'targets': _endpoint_descriptors(),
        'samsung': {
            'device_id': SAMSUNG_DEVICE_ID,
            'desired_revision': desired.get('revision'),
            'revision': receiver.get('revision'),
            'observable': bool(receiver),
        },
    })


@app.get('/api/media/history')
def media_history_get() -> Response:
    _require()
    endpoint = request.args.get('endpoint') or request.args.get('history_id') or None
    return jsonify(_history_public_items(endpoint))


@app.get('/api/media/history/<history_key>')
def media_history_item_get(history_key: str) -> Response:
    _require()
    endpoint = request.args.get('endpoint') or request.args.get('history_id') or None
    item = _history_find(endpoint, history_key)
    if not item:
        return jsonify({'error': 'Запис історії не знайдено.', 'history_id': _resolve_history_id(endpoint)}), 404
    return jsonify({'history_id': _resolve_history_id(endpoint), 'item': item})


@app.delete('/api/media/history/<history_key>')
def media_history_item_delete(history_key: str) -> Response:
    _require()
    endpoint = request.args.get('endpoint') or request.args.get('history_id') or None
    return jsonify({'status': 'ok', **_history_delete(endpoint, history_key)})


@app.post('/api/media/history/<history_key>/resume')
@app.post('/api/media/history/resume')
def media_history_resume_post(history_key: str | None = None) -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    key = history_key or str(data.get('history_key') or data.get('key') or '')
    endpoint = data.get('endpoint') or data.get('history_id') or request.args.get('endpoint') or None
    try:
        return jsonify({'status': 'ok', **_history_resume(endpoint, key)})
    except ValueError as exc:
        return jsonify({'error': str(exc), 'history_id': _resolve_history_id(endpoint)}), 404
    except Exception as exc:
        return jsonify({'error': str(exc), 'history_id': _resolve_history_id(endpoint)}), 409


@app.put('/api/media/target')
@app.post('/api/media/target')
def media_target_set() -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    try:
        action = str(data.get('action') or data.get('command') or 'select').strip().lower()
        if action in {'capture', 'charge'}:
            result = _charge_media_capture(str(data.get('source') or data.get('source_endpoint') or '') or None)
        elif action in {'handoff', 'move', 'discharge'}:
            result = _handoff_capture_to_endpoint(str(data.get('target') or ''))
        else:
            result = _select_media_target(str(data.get('target') or ''))
        return jsonify({'status': 'ok', **result})
    except ValueError as exc:
        return jsonify({'error': str(exc), **_media_target_state()}), 400
    except Exception as exc:
        return jsonify({'error': str(exc), **_media_target_state()}), 409


@app.post('/api/media/handoff')
def media_handoff_post() -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    try:
        target = str(data.get('target') or data.get('destination') or '')
        if data.get('capture') or str(data.get('action') or '').strip().lower() in {'capture', 'charge'}:
            return jsonify({'status': 'ok', **_charge_media_capture(str(data.get('source') or data.get('source_endpoint') or '') or None)})
        return jsonify({'status': 'ok', **_handoff_capture_to_endpoint(target)})
    except ValueError as exc:
        return jsonify({'error': str(exc), **_media_target_state()}), 400
    except Exception as exc:
        return jsonify({'error': str(exc), **_media_target_state()}), 409


@app.get('/api/samsung/media/desired')
def samsung_desired_get() -> Response:
    if _request_device_id() != SAMSUNG_DEVICE_ID:
        abort(403, description='Samsung media endpoint is only available to the confirmed Samsung receiver.')
    desired = _read_json(SAMSUNG_RECEIVER_DESIRED)
    if not desired:
        return jsonify({'schema': 'skeleton.samsung.media_desired.v1', 'revision': 0, 'mode': 'idle', 'updated_at': 0})
    return jsonify(desired)


def _incoming_has_playback_fields(data: dict) -> bool:
    return any(key in data for key in ('playing', 'position_seconds', 'position', 'duration_seconds'))


@app.post('/api/samsung/media/status')
def samsung_status_post() -> Response:
    device_id = _request_device_id()
    if device_id != SAMSUNG_DEVICE_ID:
        abort(403, description='Samsung media endpoint is only available to the confirmed Samsung receiver.')
    data = request.get_json(silent=True) or {}
    previous = _samsung_receiver_status()
    revision = _optional_int(data.get('revision')) or 0
    allowed = dict(previous)
    if revision != int(previous.get('revision') or 0) and not _incoming_has_playback_fields(data):
        for key in ('playing', 'position_seconds', 'position', 'duration_seconds'):
            allowed.pop(key, None)
    allowed.update({
        'schema': 'skeleton.samsung.media_status.v1',
        'device_id': device_id,
        'mode': str(data.get('mode') or previous.get('mode') or '')[:40],
        'revision': revision,
        'updated_at': int(time.time()),
        'app': str(data.get('app') or previous.get('app') or '')[:80],
    })
    if 'playing' in data:
        allowed['playing'] = bool(data.get('playing'))
    if 'position_seconds' in data or 'position' in data:
        allowed['position_seconds'] = float(data.get('position_seconds') if 'position_seconds' in data else data.get('position') or 0.0)
    if 'duration_seconds' in data:
        allowed['duration_seconds'] = float(data.get('duration_seconds') or 0.0)
    if data.get('error'):
        allowed['error'] = str(data.get('error'))[:500]
    else:
        allowed.pop('error', None)
    _atomic(SAMSUNG_RECEIVER_STATUS, allowed)
    media = _receiver_media(allowed)
    if media.get('job_id') or _read_json(SAMSUNG_RECEIVER_DESIRED).get('job_id'):
        desired = _read_json(SAMSUNG_RECEIVER_DESIRED)
        merged_media = {**desired, **media}
        try:
            job, source = _find_job_source(merged_media.get('job_id'), merged_media.get('source_id'))
        except Exception:
            job, source = {}, {}
        _record_history_progress(SAMSUNG_DEVICE_ID, merged_media, job=job, source=source, reason='samsung_periodic')
    return jsonify({'status': 'ok', **allowed})


@app.post('/api/jobs/<job_id>/refresh')
def refresh(job_id: str) -> Response:
    _require()
    job = _load(job_id)
    job.update({'status': 'resolving', 'sources': [], 'error': None})
    _save(job)
    pool.submit(_resolve_job, job_id)
    return jsonify({'accepted': True})


@app.post('/api/sites/confirm')
def confirm_site() -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    job = _load(str(data.get('job_id') or ''))
    candidate = job.get('site_candidate')
    if job.get('status') != 'ready' or not isinstance(candidate, dict) or not job.get('sources'):
        return jsonify({'error': 'Сайт ще не пройшов успішний аналіз.'}), 409
    try:
        entry = site_registry.confirm(candidate)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 400
    job['site_status'] = 'confirmed'
    job['site_entry'] = entry
    _save(job)
    return jsonify({'status': 'confirmed', 'site': entry})


@app.post('/api/play')
def play() -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    job = _load(str(data.get('job_id') or ''))
    source = next((s for s in job.get('sources', []) if s.get('source_id') == data.get('source_id')), None)
    if source is None:
        return jsonify({'error': 'Потік не знайдено; оновіть список.'}), 404
    try:
        result = player.play(job, source, str(data.get('subtitles') or 'off'))
        _record_history_progress(HOME_EDGE_TV_HISTORY_ID, {
            'job_id': job.get('job_id'),
            'source_id': source.get('source_id'),
            'position_seconds': 0.0,
            'playing': True,
        }, job=job, source=source, reason='tv_play')
        return jsonify({'status': 'started', 'source': {'quality': source.get('quality'), 'translation': source.get('translation')}, **result})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 502


@app.post('/api/control/<action>')
def control(action: str) -> Response:
    _require()
    try:
        result = player.control(action)
        player_status = result.get('player') if isinstance(result.get('player'), dict) else player.status()
        _record_tv_history_from_status(player_status, reason='tv_control')
        return jsonify({'status': 'ok', **result})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 404
    except Exception as exc:
        return jsonify({'error': str(exc), 'player': player.status()}), 409


@app.get('/api/volume')
def volume_state() -> Response:
    _require()
    try:
        return jsonify(_volume_status())
    except Exception as exc:
        return jsonify({'error': str(exc)}), 503


@app.post('/api/volume')
def volume_set() -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    try:
        level = int(data.get('level'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Некоректний рівень гучності.'}), 400
    if not 0 <= level <= 100:
        return jsonify({'error': 'Дозволено 0–100%.'}), 400
    try:
        return jsonify({'status': 'ok', **_set_volume(level)})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 503


@app.get('/api/hyperion')
def hyperion_state() -> Response:
    _require()
    try:
        return jsonify(_hyperion_status())
    except Exception as exc:
        return jsonify({'error': str(exc)}), 503


@app.post('/api/hyperion')
def hyperion_set() -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    enabled = data.get('enabled')
    if not isinstance(enabled, bool):
        return jsonify({'error': 'Поле enabled має бути true або false.'}), 400
    try:
        return jsonify({'status': 'ok', **_set_hyperion(enabled)})
    except Exception as exc:
        detail = str(exc)
        lowered = detail.lower()
        if 'curl:' in lowered and ('connect' in lowered or 'timeout' in lowered):
            return jsonify({
                'error': 'TV-WLED зараз недоступний. Перевірте живлення контролера підсвітки; Home Edge відновить HyperHDR автоматично після його появи.',
                'code': 'tv_wled_offline',
                'retry_automatic': True,
            }), 503
        return jsonify({'error': 'Не вдалося змінити стан підсвітки.', 'detail': detail[-300:]}), 503


@app.get('/api/mode')
def mode_state() -> Response:
    _require()
    return jsonify(player.mode_status())


@app.post('/api/mode/<mode>')
def mode_set(mode: str) -> Response:
    _require()
    try:
        with _MODE_LOCK:
            return jsonify({'status': 'ok', **player.switch_mode(mode)})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 404
    except Exception as exc:
        return jsonify({'error': str(exc), **player.mode_status()}), 409



@app.get('/api/game/status')
def game_status() -> Response:
    _require()
    return jsonify(_game_status())


@app.post('/api/game/control')
def game_control() -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    action = str(data.get('action') or '').strip().lower()
    phase = str(data.get('phase') or 'tap').strip().lower()
    try:
        return jsonify({'status': 'ok', **_game_input(action, phase)})
    except ValueError as exc:
        return jsonify({'error': str(exc), **_game_status()}), 400
    except Exception as exc:
        return jsonify({'error': str(exc), **_game_status()}), 409


@app.get('/api/remote/status')
def remote_status() -> Response:
    _require()
    try:
        return jsonify(_remote_status())
    except Exception as exc:
        return jsonify({'error': str(exc)}), 503


@app.post('/api/remote/control')
def remote_control() -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    action = str(data.get('action') or '').strip().lower()
    phase = str(data.get('phase') or 'tap').strip().lower()
    try:
        return jsonify({'status': 'ok', **_remote_control(action, phase)})
    except ValueError as exc:
        return jsonify({'error': str(exc), **_remote_status()}), 400
    except Exception as exc:
        return jsonify({'error': str(exc), **_remote_status()}), 409


@app.post('/api/remote/pointer')
def remote_pointer() -> Response:
    _require()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Некоректний payload тачпада.'}), 400
    allowed_fields = {'action', 'dx', 'dy'}
    if set(data) - allowed_fields:
        return jsonify({'error': 'Невідомі поля в payload тачпада.'}), 400
    action = str(data.get('action') or '').strip().lower()
    if action not in {'move', 'tap', 'drag_start', 'drag_end', 'scroll'}:
        return jsonify({'error': 'Невідома дія тачпада.'}), 400
    try:
        dx, dy = float(data.get('dx') or 0), float(data.get('dy') or 0)
        if not math.isfinite(dx) or not math.isfinite(dy) or abs(dx) > 120 or abs(dy) > 120:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'error': 'Некоректне переміщення тачпада.'}), 400
    try:
        return jsonify({'status': 'ok', **_remote_pointer(action, dx, dy)})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 409


@app.post('/api/remote/keyboard')
def remote_keyboard() -> Response:
    _require()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Некоректний payload клавіатури.'}), 400
    allowed_fields = {'action', 'text', 'key', 'layout'}
    if set(data) - allowed_fields:
        return jsonify({'error': 'Невідомі поля в payload клавіатури.'}), 400
    action = str(data.get('action') or '').strip().lower()
    layout = str(data.get('layout') or 'en').strip().lower()
    text = str(data.get('text') or '')
    key = str(data.get('key') or '')
    if action not in {'text', 'key'} or layout not in {'en', 'uk', 'ru'}:
        return jsonify({'error': 'Невідома дія або розкладка клавіатури.'}), 400
    if action == 'text' and (not text or len(text) > 300):
        return jsonify({'error': 'Текст має містити 1–300 символів.'}), 400
    allowed_keys = {'Enter','Backspace','Escape','Tab','Space','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'}
    if action == 'key' and key not in allowed_keys:
        return jsonify({'error': 'Клавішу не дозволено.'}), 400
    try:
        return jsonify({'status': 'ok', **_remote_keyboard(action, text, key, layout)})
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        return jsonify({'error': str(exc)}), 409


@app.get('/api/player')
def player_state() -> Response:
    _require()
    return jsonify(player.status())


@app.post('/api/seek')
def seek() -> Response:
    _require()
    data = request.get_json(silent=True) or {}
    try:
        position = float(data.get('position'))
        if not 0 <= position <= 86400:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'error': 'Некоректна позиція відтворення.'}), 400
    try:
        result = player.seek_absolute(position)
        player_status = result.get('player') if isinstance(result.get('player'), dict) else player.status()
        _record_tv_history_from_status(player_status, reason='tv_progress')
        return jsonify({'status': 'ok', **result})
    except Exception as exc:
        return jsonify({'error': str(exc), 'player': player.status()}), 409


@app.get('/select/<job_id>')
def select(job_id: str) -> str:
    _require()
    _job_path(job_id)
    return _template('select.html', __JOB_ID__=json.dumps(job_id))


if __name__ == '__main__':
    STATE.mkdir(parents=True, exist_ok=True)
    JOBS.mkdir(parents=True, exist_ok=True)
    app.run(host='0.0.0.0', port=PORT, threaded=True, use_reloader=False)
