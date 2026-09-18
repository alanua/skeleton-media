from __future__ import annotations
from contextvars import ContextVar

import datetime as dt
import difflib
import hashlib
import html
import json
import os
import re
import sqlite3
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, unquote
from urllib.request import Request, urlopen

import requests
from bs4 import BeautifulSoup

from . import resolver
from . import site_registry

HOME = Path(os.environ.get('SKELETON_MEDIA_HOME', str(Path.home()))).expanduser()
STATE = HOME / '.local/state/skeleton-cast'
DB = STATE / 'media-catalog.sqlite3'
QWEN_CONFIG = HOME / '.config/skeleton/local-inference-bridge.json'
OVERVIEW_TRANSLATIONS = STATE / 'media-overview-translations.json'
LANGUAGE_PREFERENCES = STATE / 'language-preferences.json'
SEASON_METADATA_CACHE = STATE / 'tmdb-season-metadata.json'
TMDB_UI_CACHE = STATE / 'tmdb-ui-metadata.json'
RATINGS_CACHE = STATE / 'media-ratings-cache.json'
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145 Safari/537.36'
PLAYABLE_SEARCH_DOMAINS = (
    'uakino.best','uakino.me','uakino.club','klon.fun','lavakino.net','anitube.in.ua','kinogo.online',
    'uaserial.com','uaserial.tv','uafix.net','eneyida.tv',
)
AVAILABILITY_SEARCH_DOMAINS = (
    'megogo.net','sweet.tv','takflix.com','kinorium.com','trakt.tv','app.trakt.tv',
)
SEARCH_DOMAINS = PLAYABLE_SEARCH_DOMAINS
DEDICATED_PLAYABLE_SEARCH_DOMAINS = ('uakino.best','uaserial.tv','uafix.net','eneyida.tv')
CROSS_SEARCH_DOMAINS = ('uaserial.tv','uafix.net','eneyida.tv','uakino.best','lavakino.net','kinogo.online','anitube.in.ua')
SEARCH_VERSION = 'v130'
SEARCH_ALIAS_LOCALES = ('en-US','ru-RU','de-DE','fr-FR','es-ES')
NEGATIVE_CACHE_SECONDS = 6 * 60 * 60
INTERACTIVE_SEARCH_TIMEOUT = 30
UAKINO_SEARCH_TIMEOUT = 12
BRAVE_SEARCH_ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'
BRAVE_KEY_FILE_DEFAULT = HOME / '.config/skeleton-cast/brave-search.key'

CATALOG_HOSTS = {
    'imdb.com', 'www.imdb.com', 'm.imdb.com',
    'themoviedb.org', 'www.themoviedb.org',
    'trakt.tv', 'app.trakt.tv',
    'moviebase.app', 'www.moviebase.app',
    'megogo.net', 'www.megogo.net', 'sweet.tv', 'www.sweet.tv',
    'takflix.com', 'www.takflix.com', 'kinorium.com', 'www.kinorium.com',
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    con.executescript('''
      PRAGMA journal_mode=WAL;
      PRAGMA foreign_keys=ON;
      CREATE TABLE IF NOT EXISTS media_items (
        media_uid TEXT PRIMARY KEY,
        source_url TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL,
        media_type TEXT NOT NULL,
        title TEXT NOT NULL,
        original_title TEXT,
        release_year INTEGER,
        tmdb_id INTEGER,
        imdb_id TEXT,
        poster TEXT,
        overview TEXT,
        metadata_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS media_items_title_year_idx ON media_items(title, release_year);
      CREATE TABLE IF NOT EXISTS media_aliases (
        provider TEXT NOT NULL,
        external_id TEXT NOT NULL,
        media_type TEXT NOT NULL,
        media_uid TEXT NOT NULL,
        source_url TEXT,
        confirmed INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(provider, external_id, media_type),
        FOREIGN KEY(media_uid) REFERENCES media_items(media_uid) ON DELETE CASCADE
      );
      CREATE INDEX IF NOT EXISTS media_aliases_uid_idx ON media_aliases(media_uid);
      CREATE TABLE IF NOT EXISTS discoveries (
        discovery_id TEXT PRIMARY KEY,
        media_uid TEXT,
        catalog_url TEXT NOT NULL,
        selected_page_url TEXT,
        selected_score REAL,
        candidate_count INTEGER NOT NULL DEFAULT 0,
        qwen_used INTEGER NOT NULL DEFAULT 0,
        qwen_confidence REAL,
        status TEXT NOT NULL,
        details_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(media_uid) REFERENCES media_items(media_uid)
      );
    ''')
    try:
        os.chmod(DB, 0o600)
    except OSError:
        pass
    return con


def _trakt_external_id(url: str) -> tuple[str, str] | None:
    parsed = urlparse(str(url or ''))
    match = re.search(r'/(movies|shows)/([^/?#]+)', parsed.path)
    if not match:
        return None
    return f'{match.group(1)}/{match.group(2)}', ('movie' if match.group(1) == 'movies' else 'tv')


def _alias_uid(provider: str, external_id: str, media_type: str) -> str | None:
    if not provider or not external_id or not media_type:
        return None
    try:
        with _db() as con:
            row = con.execute('SELECT media_uid FROM media_aliases WHERE provider=? AND external_id=? AND media_type=?', (provider, str(external_id), media_type)).fetchone()
        return str(row['media_uid']) if row else None
    except Exception:
        return None


def _alias_metadata(provider: str, external_id: str, media_type: str) -> dict[str, Any] | None:
    uid = _alias_uid(provider, external_id, media_type)
    if not uid:
        return None
    try:
        with _db() as con:
            row = con.execute('SELECT metadata_json,title,original_title,release_year,tmdb_id,imdb_id,poster,overview FROM media_items WHERE media_uid=?', (uid,)).fetchone()
        if not row:
            return None
        try:
            meta = json.loads(str(row['metadata_json'] or '{}'))
        except Exception:
            meta = {}
        meta.update({'media_uid':uid,'media_type':media_type,'title':str(row['title'] or meta.get('title') or ''),'original_title':str(row['original_title'] or meta.get('original_title') or ''),'year':int(row['release_year'] or meta.get('year') or 0),'tmdb_id':int(row['tmdb_id'] or 0) or None,'imdb_id':str(row['imdb_id'] or '') or None,'poster':row['poster'] or meta.get('poster'),'overview':str(row['overview'] or meta.get('overview') or '')})
        return meta
    except Exception:
        return None


def _canonical_alias_uid(meta: dict[str, Any], catalog_url: str = '') -> str | None:
    media_type = str(meta.get('media_type') or '')
    tmdb_id = int(meta.get('tmdb_id') or 0)
    if tmdb_id and media_type:
        uid = _alias_uid('tmdb', str(tmdb_id), media_type)
        if uid:
            return uid
    imdb_id = str(meta.get('imdb_id') or '')
    if imdb_id and media_type:
        uid = _alias_uid('imdb', imdb_id, media_type)
        if uid:
            return uid
    trakt = _trakt_external_id(catalog_url or str(meta.get('source_url') or ''))
    if trakt:
        uid = _alias_uid('trakt', trakt[0], trakt[1])
        if uid:
            return uid
    return None


def _persist_aliases(con: sqlite3.Connection, meta: dict[str, Any], catalog_url: str, *, confirmed: bool = False) -> None:
    uid = str(meta.get('media_uid') or '')
    media_type = str(meta.get('media_type') or '')
    if not uid or not media_type:
        return
    stamp = now()
    aliases = []
    tmdb_id = int(meta.get('tmdb_id') or 0)
    if tmdb_id:
        aliases.append(('tmdb', str(tmdb_id), media_type))
    imdb_id = str(meta.get('imdb_id') or '')
    if imdb_id:
        aliases.append(('imdb', imdb_id, media_type))
    for u in (catalog_url, str(meta.get('source_url') or '')):
        trakt = _trakt_external_id(u)
        if trakt:
            aliases.append(('trakt', trakt[0], trakt[1]))
    sql = 'INSERT INTO media_aliases(provider,external_id,media_type,media_uid,source_url,confirmed,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(provider,external_id,media_type) DO UPDATE SET media_uid=excluded.media_uid,source_url=COALESCE(excluded.source_url,media_aliases.source_url),confirmed=MAX(media_aliases.confirmed,excluded.confirmed),updated_at=excluded.updated_at'
    for provider, external_id, kind in dict.fromkeys(aliases):
        con.execute(sql, (provider, external_id, kind, uid, catalog_url or None, 1 if confirmed else 0, stamp))


def _normal(value: str) -> str:
    value = unicodedata.normalize('NFKD', str(value or '')).casefold()
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r'[^\w\s]+', ' ', value, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', value).strip()


def _year(value: Any) -> int:
    match = re.search(r'\b(19\d{2}|20\d{2})\b', str(value or ''))
    return int(match.group(1)) if match else 0


def _host(url: str) -> str:
    return (urlparse(url).hostname or '').lower().rstrip('.')


def is_catalog_url(url: str) -> bool:
    host = _host(url)
    return host in CATALOG_HOSTS or any(host.endswith('.' + item) for item in CATALOG_HOSTS)


def _proxy(path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    try:
        url = 'http://127.0.0.1:8102' + path + '?' + urlencode({key: value for key, value in params.items() if value not in (None, '')})
        with urlopen(url, timeout=24) as response:
            data = json.loads(response.read().decode('utf-8'))
        return data if isinstance(data, dict) and not data.get('error') else None
    except Exception:
        return None



_CONTENT_LANGUAGE_CONTEXT: ContextVar[str] = ContextVar('skeleton_content_language', default='')

def push_content_language(language: str):
    value=str(language or 'uk').lower().split('-',1)[0]
    if value not in {'uk','ru','en','de'}: value='uk'
    return _CONTENT_LANGUAGE_CONTEXT.set(value)

def pop_content_language(token) -> None:
    _CONTENT_LANGUAGE_CONTEXT.reset(token)

def preferred_content_language() -> str:
    contextual=_CONTENT_LANGUAGE_CONTEXT.get().strip().lower()
    if contextual in {'uk','ru','en','de'}:
        return contextual
    # Default only; user/device preference is stamped into each job by app.py.
    try:
        data=json.loads(LANGUAGE_PREFERENCES.read_text(encoding='utf-8'))
        defaults=data.get('defaults') if isinstance(data.get('defaults'),dict) else data
        value=str(defaults.get('content_language') or 'uk').lower().split('-',1)[0]
    except Exception:
        value='uk'
    return value if value in {'uk','ru','en','de'} else 'uk'

def preferred_content_locale() -> str:
    return {'uk':'uk-UA','ru':'ru-RU','en':'en-US','de':'de-DE'}.get(preferred_content_language(),'uk-UA')

def preferred_accept_language() -> str:
    lang=preferred_content_language(); loc=preferred_content_locale()
    return f'{loc},{lang};q=0.9,en;q=0.7' if lang!='en' else 'en-US,en;q=0.9'

def content_search_suffix() -> str:
    return {'uk':'дивитися українською','ru':'смотреть на русском','en':'watch English','de':'auf Deutsch ansehen'}.get(preferred_content_language(),'дивитися українською')

def _overview_cache_read() -> dict[str, Any]:
    try:
        data = json.loads(OVERVIEW_TRANSLATIONS.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _overview_cache_write(data: dict[str, Any]) -> None:
    OVERVIEW_TRANSLATIONS.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERVIEW_TRANSLATIONS.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, OVERVIEW_TRANSLATIONS)
    os.chmod(OVERVIEW_TRANSLATIONS, 0o600)


def _translation_quality(value: str, source: str) -> bool:
    text = str(value or '').strip()
    if len(text) < 24 or len(text) > max(1200, len(source) * 3):
        return False
    letters = [ch for ch in text if ch.isalpha()]
    cyrillic = sum('\u0400' <= ch <= '\u04ff' for ch in letters)
    if not letters or cyrillic / len(letters) < 0.72:
        return False
    bad = ('модой', 'гучніх', 'арт-сфери', 'визначають серію', '\x1b', '```')
    return not any(token in text.casefold() for token in bad)


def _local_translate_uk(value: str, source_language: str = 'auto') -> str:
    source = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not source:
        return ''
    language = str(source_language or 'auto').strip().lower()
    key_id = hashlib.sha256(f'skeleton-translation-gateway-v1.2|{language}|{source}'.encode('utf-8')).hexdigest()
    cache = _overview_cache_read()
    record = cache.get(key_id) if isinstance(cache.get(key_id), dict) else {}
    cached = str(record.get('translation') or '').strip()
    if cached and str(record.get('provider') or '').startswith('skeleton_translation_gateway') and _translation_quality(cached, source):
        return cached
    try:
        payload = json.dumps({
            'text': source,
            'source_language': language,
            'target_language': 'uk',
            'domain': 'media',
            'quality': 'high',
        }, ensure_ascii=False).encode('utf-8')
        request = Request(
            'http://127.0.0.1:8771/v1/translate', data=payload,
            headers={'Content-Type': 'application/json'}, method='POST',
        )
        with urlopen(request, timeout=300) as response:
            result = json.loads(response.read().decode('utf-8'))
        translated = re.sub(r'\s+', ' ', str(result.get('translation') or '')).strip()
        if result.get('status') != 'DONE' or result.get('review_required') or not _translation_quality(translated, source):
            return ''
        cache[key_id] = {
            'source': source, 'translation': translated,
            'source_language': str(result.get('source_language') or language),
            'target_language': 'uk',
            'provider': 'skeleton_translation_gateway_v1.2',
            'quality_score': result.get('quality_score'),
            'updated_at': now(),
        }
        _overview_cache_write(cache)
        return translated
    except Exception:
        return ''


def _tmdb_language_locale(language: str) -> str:
    code = str(language or '').strip().lower().split('-', 1)[0]
    return {
        'uk': 'uk-UA', 'de': 'de-DE', 'en': 'en-US', 'it': 'it-IT',
        'ru': 'ru-RU', 'es': 'es-ES', 'fr': 'fr-FR', 'pl': 'pl-PL',
        'pt': 'pt-PT', 'nl': 'nl-NL', 'tr': 'tr-TR', 'ja': 'ja-JP',
        'ko': 'ko-KR', 'zh': 'zh-CN',
    }.get(code, code or 'en-US')


def _tmdb_overview_with_local_translation(media_type: str, tmdb_id: int, uk_overview: str) -> tuple[str, str]:
    current = str(uk_overview or '').strip()
    if current:
        return current, 'tmdb_uk'
    base = _proxy('/v1/item', {'media_type': media_type, 'tmdb_id': tmdb_id, 'language': 'uk-UA'}) or {}
    original_language = str(base.get('language') or '').strip().lower().split('-', 1)[0]
    attempts: list[tuple[str, str]] = []
    if original_language and original_language != 'uk':
        attempts.append((original_language, _tmdb_language_locale(original_language)))
    for language, locale in (('ru', 'ru-RU'), ('en', 'en-US')):
        if language not in {item[0] for item in attempts}:
            attempts.append((language, locale))
    for language, locale in attempts:
        item = _proxy('/v1/item', {'media_type': media_type, 'tmdb_id': tmdb_id, 'language': locale}) or {}
        source = str(item.get('overview') or '').strip()
        if not source:
            continue
        translated = _local_translate_uk(source, language)
        if translated:
            return translated, f'tmdb_{language}_skeleton_translation_gateway_uk'
    return '', 'tmdb_missing'


def _proxy_metadata(data: dict[str, Any], *, provider: str, catalog_url: str) -> dict[str, Any]:
    poster = resolver._cache_poster(str(data.get('poster_url') or ''), 'https://www.themoviedb.org/') if data.get('poster_url') else None
    return {
        'provider': provider, 'catalog_provider': provider, 'catalog_url': catalog_url,
        'source_url': catalog_url, 'media_type': str(data.get('media_type') or 'unknown'),
        'tmdb_id': int(data.get('tmdb_id') or 0) or None,
        'imdb_id': str(data.get('imdb_id') or '') or None,
        'title': str(data.get('title') or 'Без назви'),
        'original_title': str(data.get('original_title') or ''),
        'year': int(data.get('year') or 0), 'overview': str(data.get('overview') or ''),
        'poster': poster, 'metadata_transport': 'tmdb_private_proxy',
    }


def _season_metadata_cache_read() -> dict[str, Any]:
    try:
        data = json.loads(SEASON_METADATA_CACHE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _season_metadata_cache_write(data: dict[str, Any]) -> None:
    SEASON_METADATA_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEASON_METADATA_CACHE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, SEASON_METADATA_CACHE)
    os.chmod(SEASON_METADATA_CACHE, 0o600)


def tmdb_season_metadata(tmdb_id: int, season_number: int, *, language: str = 'uk-UA', force: bool = False) -> dict[str, Any]:
    series_id = int(tmdb_id or 0)
    season = int(season_number or 0)
    if series_id <= 0 or season <= 0:
        return {}
    key = f'{series_id}:{season}:{language}'
    cache = _season_metadata_cache_read()
    record = cache.get(key) if isinstance(cache.get(key), dict) else {}
    cached_at = int(record.get('cached_at') or 0)
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    if record and not force and now_ts - cached_at < 30 * 24 * 60 * 60 and int(record.get('episode_count') or 0) > 0:
        return dict(record)
    url = f'https://www.themoviedb.org/tv/{series_id}/season/{season}?language={quote(language)}'
    try:
        response = _get(url, timeout=30)
        soup = BeautifulSoup(response.text, 'lxml')
        def og(name: str) -> str:
            tag = soup.find('meta', attrs={'property': name})
            return str(tag.get('content') or '').strip() if tag else ''
        page_title = soup.title.get_text(' ', strip=True) if soup.title else ''
        image = og('og:image')
        poster = resolver._cache_poster(image, response.url) if image else None
        overview = og('og:description')
        if overview and language.lower().startswith('uk') and not re.search(r'[А-Яа-яІіЇїЄєҐґ]', overview):
            translated = _local_translate_uk(overview)
            if translated:
                overview = translated
        episode_numbers = sorted({
            int(match.group(1))
            for anchor in soup.find_all('a', href=True)
            for match in [re.search(rf'/tv/{series_id}[^/]*/season/{season}/episode/(\d+)', str(anchor.get('href') or ''))]
            if match and int(match.group(1)) > 0
        })
        record = {
            'tmdb_id': series_id,
            'season': season,
            'title': og('og:title') or f'Сезон {season}',
            'year': _year(page_title),
            'poster': poster,
            'overview': overview,
            'episode_numbers': episode_numbers,
            'episode_count': len(episode_numbers),
            'source_url': response.url,
            'language': language,
            'cached_at': now_ts,
        }
        cache[key] = record
        _season_metadata_cache_write(cache)
        return dict(record)
    except Exception:
        return dict(record) if record else {}


def tmdb_series_season_catalog(tmdb_id: int, loaded_seasons: list[str] | set[str] | tuple[str, ...] = (), *, allow_network: bool = False) -> list[dict[str, Any]]:
    series_id = int(tmdb_id or 0)
    if series_id <= 0:
        return []
    cache = _season_metadata_cache_read()
    by_season: dict[int, dict[str, Any]] = {}
    # Prefer Ukrainian cache rows; English is a safe fallback for missing Ukrainian records.
    for key, record in cache.items():
        if not isinstance(record, dict) or not str(key).startswith(f'{series_id}:'):
            continue
        try:
            season = int(record.get('season') or str(key).split(':', 2)[1])
        except Exception:
            continue
        if season <= 0 or int(record.get('episode_count') or 0) <= 0:
            continue
        old = by_season.get(season)
        if old is None or str(record.get('language') or '').lower().startswith('uk'):
            by_season[season] = dict(record)
    loaded = {int(str(v)) for v in loaded_seasons if str(v).isdigit() and int(str(v)) > 0}
    if allow_network:
        # One-time direct TMDB catalogue enumeration. This never invokes a web-search provider.
        # Seasons are contiguous in TMDB; two misses after the highest known season terminate the probe.
        max_known = max(set(by_season) | loaded | {1})
        misses = 0
        for season in range(1, 31):
            if season in by_season:
                misses = 0
                continue
            record = tmdb_season_metadata(series_id, season)
            if int(record.get('episode_count') or 0) > 0:
                by_season[season] = record
                max_known = max(max_known, season)
                misses = 0
            elif season > max_known:
                misses += 1
                if misses >= 2:
                    break
    year_now = dt.datetime.now(dt.timezone.utc).year
    result: list[dict[str, Any]] = []
    for season in sorted(set(by_season) | loaded):
        record = by_season.get(season) or {}
        year = int(record.get('year') or 0)
        poster = str(record.get('poster') or '')
        result.append({
            'season_number': season,
            'title': str(record.get('title') or f'Сезон {season}'),
            'year': year or None,
            'poster': poster,
            'episode_count': int(record.get('episode_count') or 0),
            'released': (not year or year <= year_now),
            'loaded': season in loaded,
        })
    return result


def _tmdb_ui_cache_read() -> dict[str, Any]:
    try:
        data = json.loads(TMDB_UI_CACHE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _tmdb_ui_cache_write(data: dict[str, Any]) -> None:
    TMDB_UI_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TMDB_UI_CACHE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, TMDB_UI_CACHE)
    os.chmod(TMDB_UI_CACHE, 0o600)


def tmdb_ui_metadata(tmdb_id: int, media_type: str = 'tv', *, allow_network: bool = False) -> dict[str, Any]:
    item_id = int(tmdb_id or 0)
    kind = 'movie' if str(media_type).lower() == 'movie' else 'tv'
    if item_id <= 0:
        return {}
    key = f'{kind}:{item_id}'
    cache = _tmdb_ui_cache_read()
    row = cache.get(key) if isinstance(cache.get(key), dict) else {}
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    if row and (not allow_network or now_ts - int(row.get('cached_at') or 0) < 30 * 24 * 60 * 60):
        return dict(row)
    if not allow_network:
        return dict(row) if row else {}
    try:
        response = _get(f'https://www.themoviedb.org/{kind}/{item_id}?language=en-US', timeout=30)
        text = response.text
        rating_match = re.search(r'data-percent="([0-9.]+)"', text)
        imdb_match = re.search(r'https?://(?:www\.)?imdb\.com/title/(tt\d+)', text)
        row = {
            'tmdb_rating_percent': float(rating_match.group(1)) if rating_match else None,
            'imdb_id': imdb_match.group(1) if imdb_match else None,
            'source_url': response.url,
            'cached_at': now_ts,
        }
        cache[key] = row
        _tmdb_ui_cache_write(cache)
        return dict(row)
    except Exception:
        return dict(row) if row else {}


def tmdb_work_details(tmdb_id: int, media_type: str = 'tv', *, allow_network: bool = False) -> dict[str, Any]:
    item_id = int(tmdb_id or 0)
    kind = 'movie' if str(media_type).lower() == 'movie' else 'tv'
    if item_id <= 0:
        return {}
    cache_path = STATE / 'tmdb-work-details.json'
    try:
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
        if not isinstance(cache, dict): cache = {}
    except Exception:
        cache = {}
    key = f'{kind}:{item_id}'
    row = cache.get(key) if isinstance(cache.get(key), dict) else {}
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    runtime_complete = int(row.get('runtime_minutes') or 0) > 0
    if row and (not allow_network or (now_ts - int(row.get('cached_at') or 0) < 30 * 24 * 60 * 60 and runtime_complete)):
        return dict(row)
    if not allow_network:
        return dict(row) if row else {}
    try:
        response = _get(f'https://www.themoviedb.org/{kind}/{item_id}?language=en-US', timeout=25)
        soup = BeautifulSoup(response.text, 'lxml')
        result: dict[str, Any] = {'cached_at': now_ts, 'source_url': response.url}
        # Genres are explicit canonical TMDB links.
        genres=[]
        for a in soup.select('a[href*="/genre/"]'):
            text=' '.join(a.get_text(' ',strip=True).split())
            if text and text not in genres: genres.append(text)
        if genres: result['genres']=genres[:12]
        # Sidebar facts are labelled with strong tags on TMDB public pages.
        labels={
            'Status':'status','Type':'type','Original Language':'original_language',
            'Budget':'budget','Revenue':'revenue'
        }
        for strong in soup.find_all('strong'):
            label=' '.join(strong.get_text(' ',strip=True).split()).rstrip(':')
            key_name=labels.get(label)
            if not key_name: continue
            parent=strong.parent
            text=' '.join(parent.get_text(' ',strip=True).split()) if parent else ''
            value=text[len(label):].strip(' :') if text.startswith(label) else ''
            if value: result[key_name]=value
        # Network/studio/company names from canonical links or logo alts.
        networks=[]; companies=[]; countries=[]
        for a in soup.find_all('a', href=True):
            href=str(a.get('href') or '')
            name=' '.join(a.get_text(' ',strip=True).split())
            img=a.find('img')
            if not name and img: name=str(img.get('alt') or '').strip()
            if not name: continue
            if '/network/' in href:
                m = re.match(r'See more TV shows from (.+?)\.\.\.$', name)
                if m: name = m.group(1).strip()
                if name and name not in networks: networks.append(name)
            if '/company/' in href and name not in companies: companies.append(name)
            if '/country/' in href and name not in countries: countries.append(name)
        if networks: result['networks']=networks[:12]
        if companies: result['companies']=companies[:16]
        if countries: result['countries']=countries[:12]
        # Certification badges appear as certification spans.
        cert=next((' '.join(x.get_text(' ',strip=True).split()) for x in soup.select('.certification') if x.get_text(strip=True)), '')
        if cert: result['certification']=cert
        # Release/air date: first prominent release_date text when present.
        date_el=soup.select_one('.release_date')
        if date_el:
            date_text=' '.join(date_el.get_text(' ',strip=True).split()).strip('() ')
            if date_text: result['release_date']=date_text
        def _runtime_minutes(text: str) -> int:
            value=' '.join(str(text or '').split()).lower()
            hm=re.search(r'(?:(\d+)h\s*)?(?:(\d+)m)?', value)
            if not hm or (not hm.group(1) and not hm.group(2)): return 0
            return int(hm.group(1) or 0)*60 + int(hm.group(2) or 0)
        runtime_values=[]
        if kind == 'movie':
            runtime_values=[_runtime_minutes(x.get_text(' ',strip=True)) for x in soup.select('.runtime')]
        else:
            try:
                season_response=_get(f'https://www.themoviedb.org/tv/{item_id}/season/1?language=en-US', timeout=25)
                season_soup=BeautifulSoup(season_response.text,'lxml')
                runtime_values=[_runtime_minutes(x.get_text(' ',strip=True)) for x in season_soup.select('.episode .runtime')]
            except Exception:
                runtime_values=[]
        runtime_values=sorted(v for v in runtime_values if 1 <= v <= 600)
        if runtime_values:
            middle=len(runtime_values)//2
            runtime_minutes=runtime_values[middle] if len(runtime_values)%2 else int(round((runtime_values[middle-1]+runtime_values[middle])/2))
            result['runtime_minutes']=runtime_minutes
            result['runtime_kind']='episode_typical' if kind == 'tv' else 'movie'
            result['runtime_samples']=len(runtime_values)
            hours,minutes=divmod(runtime_minutes,60)
            label=(f'{hours} год {minutes} хв' if hours and minutes else f'{hours} год' if hours else f'{minutes} хв')
            result['runtime_label']=(f'≈{label}/серія' if kind == 'tv' else label)
        cache[key]=result
        cache_path.parent.mkdir(parents=True,exist_ok=True)
        tmp=cache_path.with_suffix('.json.tmp'); tmp.write_text(json.dumps(cache,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); os.chmod(tmp,0o600); os.replace(tmp,cache_path)
        return dict(result)
    except Exception:
        return dict(row) if row else {}


def _ratings_cache_read() -> dict[str, Any]:
    try:
        data = json.loads(RATINGS_CACHE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _ratings_cache_write(data: dict[str, Any]) -> None:
    RATINGS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RATINGS_CACHE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    os.replace(tmp, RATINGS_CACHE)

def media_ratings_metadata(tmdb_id: int, media_type: str = 'tv', *, allow_network: bool = False) -> dict[str, Any]:
    item_id = int(tmdb_id or 0)
    kind = 'movie' if str(media_type).lower() == 'movie' else 'tv'
    if item_id <= 0:
        return {}
    key = f'{kind}:{item_id}'
    cache = _ratings_cache_read()
    row = cache.get(key) if isinstance(cache.get(key), dict) else {}
    now_ts = int(dt.datetime.now(dt.timezone.utc).timestamp())
    ratings_complete = bool(row.get('imdb_rating') is not None and row.get('trakt_rating') is not None and row.get('mdblist_rating') is not None)
    if row and (not allow_network or (now_ts - int(row.get('cached_at') or 0) < 2592000 and ratings_complete)):
        return dict(row)
    if not allow_network:
        return dict(row) if row else {}
    result = dict(row)
    ui = tmdb_ui_metadata(item_id, kind, allow_network=True)
    if ui.get('tmdb_rating_percent') is not None:
        result['tmdb_rating_percent'] = float(ui['tmdb_rating_percent'])
    if ui.get('imdb_id'):
        result['imdb_id'] = str(ui['imdb_id'])
    imdb_id = str(result.get('imdb_id') or ui.get('imdb_id') or '')
    if imdb_id.startswith('tt'):
        try:
            page_kind = 'movie' if kind == 'movie' else 'show'
            response = _get(f'https://mdblist.com/{page_kind}/{imdb_id}', timeout=20)
            text = ' '.join(BeautifulSoup(response.text, 'lxml').get_text(' ', strip=True).split())
            m = re.search(r'IMDb\s+[0-9.,KkMm]+\s+(\d+(?:\.\d+)?)', text)
            if m: result['imdb_rating'] = float(m.group(1))
            m = re.search(r'Trakt\s+(\d+(?:\.\d+)?)', text)
            if m: result['trakt_rating'] = float(m.group(1))
            m = re.search(r'MDBList\s+(\d+(?:\.\d+)?)', text, re.I)
            if m: result['mdblist_rating'] = float(m.group(1))
            m = re.search(r'TMDb\s+(\d+(?:\.\d+)?)', text)
            if m: result['tmdb_rating_percent'] = float(m.group(1))
            result['mdblist_url'] = response.url
        except Exception:
            pass
    imdb_id = str(result.get('imdb_id') or ui.get('imdb_id') or '')
    try:
            mdblist_kind = 'movie' if kind == 'movie' else 'show'
            response = _get(f'https://mdblist.com/{mdblist_kind}/tmdb/{item_id}', timeout=20)
            text = ' '.join(BeautifulSoup(response.text, 'lxml').get_text(' ', strip=True).split())
            if not imdb_id.startswith('tt'):
                id_match = re.search(r'\b(tt\d{5,})\b', response.text)
                if id_match:
                    imdb_id = id_match.group(1); result['imdb_id'] = imdb_id
            # MDbList exposes a compact rating strip: IMDb / Trakt / TMDb / ... / MDBList.
            m = re.search(r'([0-9]+(?:\.[0-9]+)?)\s+IMDb\s+[0-9.,KkMm]+\s+([0-9]+(?:\.[0-9]+)?)\s+[0-9.,KkMm]+\s+Trakt\s+([0-9]+(?:\.[0-9]+)?)\s+[0-9.,KkMm]+\s+TMDb\s+([0-9]+(?:\.[0-9]+)?)', text)
            if m:
                result['mdblist_rating'] = float(m.group(1)) * 10.0 if float(m.group(1)) <= 10 else float(m.group(1))
                result['imdb_rating'] = float(m.group(2))
                result['trakt_rating'] = float(m.group(3))
                result['tmdb_rating_percent'] = float(m.group(4))
            else:
                imdb_match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s+[0-9.,KkMm]+\s+Trakt', text)
                trakt_match = re.search(r'Trakt\s+([0-9]+(?:\.[0-9]+)?)\s+[0-9.,KkMm]+\s+TMDb', text)
                tmdb_match = re.search(r'TMDb\s+([0-9]+(?:\.[0-9]+)?)', text)
                mdbl_match = re.search(r'MDBList\s+([0-9]+(?:\.[0-9]+)?)\s+[0-9.,KkMm]+\s+votes', text, re.I)
                if imdb_match: result['imdb_rating'] = float(imdb_match.group(1))
                if trakt_match: result['trakt_rating'] = float(trakt_match.group(1))
                if tmdb_match: result['tmdb_rating_percent'] = float(tmdb_match.group(1))
                if mdbl_match:
                    value=float(mdbl_match.group(1)); result['mdblist_rating'] = value*10.0 if value<=10 else value
    except Exception:
        pass
    try:
        path_kind = 'movie' if kind == 'movie' else 'show'
        response = _get(f'https://moviebase.app/{path_kind}/{item_id}', timeout=20)
        soup = BeautifulSoup(response.text, 'lxml')
        for script in soup.find_all('script', attrs={'type':'application/ld+json'}):
            try:
                payload = json.loads(script.get_text() or '{}')
            except Exception:
                continue
            rating = payload.get('aggregateRating') if isinstance(payload, dict) else None
            if isinstance(rating, dict) and rating.get('ratingValue') is not None:
                value = float(rating.get('ratingValue')); best = float(rating.get('bestRating') or 10)
                if best > 0:
                    result['moviebase_rating'] = round(value * 100.0 / best, 1)
                    break
    except Exception:
        pass
    result['cached_at'] = now_ts
    cache[key] = result
    _ratings_cache_write(cache)
    return dict(result)

def tmdb_season_posters(tmdb_id: int, seasons: list[str] | set[str] | tuple[str, ...], *, force: bool = False) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in seasons:
        try:
            season = int(str(value))
        except (TypeError, ValueError):
            continue
        if season <= 0:
            continue
        metadata = tmdb_season_metadata(int(tmdb_id), season, force=force)
        poster = str(metadata.get('poster') or '')
        if poster:
            result[str(season)] = poster
    return result


def _get(url: str, *, timeout: int = 30) -> requests.Response:
    safe, _ = site_registry.validate_public_url(url)
    response = requests.get(
        safe,
        headers={'User-Agent': UA, 'Accept-Language': preferred_accept_language()},
        timeout=timeout,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response


def _tmdb_detail(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    match = re.search(r'^/(movie|tv)/(\d+)', parsed.path)
    if not match:
        raise ValueError('TMDB URL не містить ID фільму або серіалу.')
    media_type, tmdb_id = match.group(1), int(match.group(2))
    locale=preferred_content_locale()
    detail_url = f'https://www.themoviedb.org/{media_type}/{tmdb_id}?language={locale}'
    proxied = _proxy('/v1/item', {'media_type': media_type, 'tmdb_id': tmdb_id, 'language': locale})
    if proxied:
        metadata = _proxy_metadata(proxied, provider='tmdb', catalog_url=detail_url)
        overview, overview_source = _tmdb_overview_with_local_translation(media_type, tmdb_id, str(metadata.get('overview') or ''))
        metadata['overview'] = overview
        metadata['overview_source'] = overview_source
        return metadata
    response = _get(detail_url)
    soup = BeautifulSoup(response.text, 'lxml')
    og = {}
    for key in ('og:title', 'og:description', 'og:image', 'og:url'):
        tag = soup.find('meta', attrs={'property': key})
        og[key] = str(tag.get('content') or '').strip() if tag else ''
    page_title = soup.title.get_text(' ', strip=True) if soup.title else ''
    year = _year(page_title)
    title = og['og:title'] or re.sub(r'\s*\([^)]*\)\s*$', '', page_title).strip()
    original_title = ''
    public_aliases: list[str] = []
    for script in soup.find_all('script', attrs={'type': 'application/ld+json'}):
        raw = script.get_text(' ', strip=True)
        raw = re.sub(r'^\s*/\*\s*<!\[CDATA\[\s*\*/\s*', '', raw)
        raw = re.sub(r'\s*/\*\s*\]\]>\s*\*/\s*$', '', raw)
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict) and data.get('@type') in {'Movie', 'TVSeries'}:
            title = str(data.get('name') or title).strip()
            original_title = str(data.get('alternateName') or '').strip()
            year = year or _year(data.get('datePublished') or data.get('startDate'))
            schema_url = str(data.get('url') or '').strip()
            slug_match = re.search(r'/(?:movie|tv)/\d+-([^/?#]+)', urlparse(schema_url).path)
            if slug_match:
                slug_title = unquote(slug_match.group(1)).replace('-', ' ').strip()
                if slug_title and _normal(slug_title) != _normal(title):
                    public_aliases.append(slug_title)
            break
    poster = None
    if og['og:image']:
        poster = resolver._cache_poster(og['og:image'], detail_url)
    return {
        'provider': 'tmdb', 'source_url': detail_url,
        'media_type': media_type, 'tmdb_id': tmdb_id,
        'title': title or 'Без назви', 'original_title': original_title,
        'search_aliases': public_aliases,
        'year': year, 'overview': og['og:description'], 'poster': poster,
    }


def _tmdb_public_search(title: str, year: int = 0, media_type: str = '', *, require_unique_exact: bool = False) -> dict[str, Any] | None:
    query = str(title or '').strip()
    if not query:
        return None
    url = 'https://www.themoviedb.org/search?query=' + quote(query) + '&language=' + quote(preferred_content_locale())
    response = _get(url)
    soup = BeautifulSoup(response.text, 'lxml')
    scored: dict[str, float] = {}
    exact_matches: set[str] = set()
    target = _normal(query)
    for anchor in soup.find_all('a', href=True):
        href = str(anchor['href'])
        match = re.match(r'^/(movie|tv)/(\d+)', href)
        if not match:
            continue
        full = urljoin(response.url, href)
        key = full.split('?', 1)[0]
        text = ' '.join(anchor.get_text(' ', strip=True).split())
        card = anchor.find_parent('div', class_=lambda value: value and ('card' in value if isinstance(value, str) else 'card' in value))
        context = ' '.join((card or anchor.parent or anchor).get_text(' ', strip=True).split())
        cand_year = _year(context)
        compare_text = text or context
        normalized_text = _normal(compare_text)
        aliases = [normalized_text]
        aliases.extend(_normal(value) for value in re.findall(r'\(([^()]*)\)', compare_text))
        if '(' in compare_text:
            aliases.append(_normal(compare_text.split('(', 1)[0]))
        aliases = [value for value in aliases if value]
        exact_title = bool(target and target in aliases)
        exact_year = bool(not year or cand_year == year)
        exact_type = bool(not media_type or media_type == match.group(1))
        if exact_title and exact_year and exact_type:
            exact_matches.add(key)
        score = max((difflib.SequenceMatcher(None, target, value).ratio() for value in aliases), default=0.0)
        if target and target in aliases:
            score += 0.65
        elif target and target in normalized_text:
            score += 0.08
        if year and cand_year == year:
            score += 0.55
        elif year and cand_year:
            score -= min(0.35, abs(cand_year - year) * 0.05)
        if media_type and media_type == match.group(1):
            score += 0.18
        elif media_type:
            score -= 0.15
        scored[key] = max(score, scored.get(key, float('-inf')))
    if not scored:
        return None
    if require_unique_exact:
        if len(exact_matches) != 1:
            return None
        return _tmdb_detail(next(iter(exact_matches)))
    candidates = sorted(((score, key) for key, score in scored.items()), reverse=True)
    return _tmdb_detail(candidates[0][1])


def metadata_for_title(title: str, year: int = 0, media_type: str = '') -> dict[str, Any] | None:
    query = str(title or '').strip()
    if not query:
        return None
    target = _normal(query)
    best: tuple[float, dict[str, Any]] | None = None
    try:
        with _db() as con:
            rows = con.execute('SELECT title, original_title, release_year, media_type, metadata_json FROM media_items').fetchall()
        for row in rows:
            aliases = [_normal(row['title']), _normal(row['original_title'])]
            ratio = max((difflib.SequenceMatcher(None, target, alias).ratio() for alias in aliases if alias), default=0.0)
            score = ratio
            row_year = int(row['release_year'] or 0)
            if year and row_year == year:
                score += 0.35
            elif year and row_year:
                score -= min(0.25, abs(row_year - year) * 0.04)
            if media_type and str(row['media_type'] or '') == media_type:
                score += 0.12
            try:
                payload = json.loads(row['metadata_json'] or '{}')
            except Exception:
                continue
            if isinstance(payload, dict) and (best is None or score > best[0]):
                best = (score, payload)
    except Exception:
        best = None
    if best and best[0] >= (1.08 if year else 0.96):
        result = dict(best[1])
    else:
        result = _tmdb_public_search(query, year, media_type) or {}
    if not result:
        return None
    uid_seed = f"{result.get('tmdb_id') or ''}|{result.get('imdb_id') or ''}|{result.get('media_type') or media_type}|{result.get('title') or query}|{result.get('year') or year}"
    result['media_uid'] = str(result.get('media_uid') or ('MED-' + hashlib.sha256(uid_seed.encode()).hexdigest()[:16].upper()))
    result.setdefault('catalog_provider', result.get('provider') or 'tmdb')
    result.setdefault('media_type', media_type or 'unknown')
    result.setdefault('title', query)
    result.setdefault('year', year)
    result.setdefault('poster', None)
    result.setdefault('overview', '')
    return result


def _imdb_metadata(url: str) -> dict[str, Any]:
    match = re.search(r'/title/(tt\d+)', url)
    if not match:
        raise ValueError('IMDb URL не містить title ID.')
    imdb_id = match.group(1)
    proxied = _proxy('/v1/find', {'imdb_id': imdb_id, 'language': preferred_content_locale()})
    if proxied:
        result = _proxy_metadata(proxied, provider='imdb', catalog_url=url)
        result['imdb_id'] = imdb_id
        return result
    mirror = _get('https://r.jina.ai/https://www.imdb.com/title/' + imdb_id + '/', timeout=45)
    first = next((line.strip() for line in mirror.text.splitlines() if line.strip().startswith('Title:')), '')
    text = re.sub(r'^Title:\s*', '', first)
    text = re.sub(r'\s*⭐.*$', '', text).strip()
    year = _year(text)
    title = re.sub(r'\s*\((?:19|20)\d{2}\)\s*$', '', text).strip()
    tmdb = _tmdb_public_search(title, year) or {}
    tmdb.update({'provider': 'imdb', 'catalog_provider': 'imdb', 'catalog_url': url, 'imdb_id': imdb_id})
    tmdb.setdefault('source_url', url)
    tmdb.setdefault('media_type', 'unknown')
    tmdb.setdefault('title', title or imdb_id)
    tmdb.setdefault('original_title', title)
    tmdb.setdefault('year', year)
    tmdb.setdefault('poster', None)
    tmdb.setdefault('overview', '')
    return tmdb


def _trakt_metadata(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    match = re.search(r'/(movies|shows)/([^/?#]+)', parsed.path)
    if not match:
        raise ValueError('Trakt URL не містить фільм або серіал.')
    media_type = 'movie' if match.group(1) == 'movies' else 'tv'
    slug = match.group(2)
    trailing_year = re.search(r'-(19\d{2}|20\d{2})$', slug)
    year = int(trailing_year.group(1)) if trailing_year else 0
    title = re.sub(r'-(?:19|20)\d{2}$', '', slug).replace('-', ' ').strip()
    alias = _alias_metadata('trakt', f'{match.group(1)}/{slug}', media_type)
    if alias:
        alias.update({'provider':'trakt','catalog_provider':'trakt','catalog_url':url,'source_url':url,'identity_resolution':'provider_alias_exact'})
        if year:
            alias['year'] = year
        aliases = list(alias.get('search_aliases') or [])
        if title and title.casefold() not in {str(x).casefold() for x in aliases}:
            aliases.append(title)
        alias['search_aliases'] = aliases
        if not alias.get('original_title'):
            alias['original_title'] = title
        return _ensure_uk_overview(alias)
    tmdb = _tmdb_public_search(title, year, media_type, require_unique_exact=True) or {}
    tmdb.update({'provider': 'trakt', 'catalog_provider': 'trakt', 'catalog_url': url, 'source_url': url})
    if not tmdb.get('tmdb_id') and not tmdb.get('imdb_id'):
        tmdb['identity_resolution'] = 'trakt_slug_unresolved_or_ambiguous'
    tmdb.setdefault('media_type', media_type)
    tmdb.setdefault('title', title.title())
    tmdb.setdefault('original_title', title)
    tmdb.setdefault('year', year)
    tmdb.setdefault('poster', None)
    tmdb.setdefault('overview', '')
    return tmdb


def _moviebase_metadata(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    imdb = str((query.get('imdb') or query.get('imdb_id') or [''])[0])
    tmdb = str((query.get('tmdb') or query.get('tmdb_id') or [''])[0])
    kind = str((query.get('type') or ['movie'])[0]).lower()
    parts = [part for part in parsed.path.split('/') if part]
    requested_season = None
    requested_episode = None
    # Moviebase work and episode links use the TMDB id immediately after
    # /show|tv|series/ or /movie/. Episode links append /sXXeYY, e.g.
    # /show/615/s11e4. The old tail-only parser treated `s11e4` as a title
    # and web-searched it, which could cross-match an unrelated show.
    if not tmdb and len(parts) >= 2 and parts[0].lower() in {'movie','movies','tv','show','shows','series'} and parts[1].isdigit():
        path_kind = parts[0].lower()
        tmdb = parts[1]
        kind = 'tv' if path_kind in {'tv','show','shows','series'} else 'movie'
        if kind == 'tv' and len(parts) >= 3:
            ep = re.fullmatch(r'(?i)s(\d{1,3})e(\d{1,4})', parts[2])
            if ep:
                requested_season = int(ep.group(1))
                requested_episode = int(ep.group(2))
    elif not tmdb and parts and parts[-1].isdigit():
        path_kind = parts[-2].lower() if len(parts) >= 2 else ''
        if path_kind in {'movie','movies','tv','show','shows','series'}:
            tmdb = parts[-1]
            kind = 'tv' if path_kind in {'tv','show','shows','series'} else 'movie'
    if imdb.startswith('tt'):
        return _imdb_metadata('https://www.imdb.com/title/' + imdb + '/')
    if tmdb.isdigit():
        result = _tmdb_detail(f'https://www.themoviedb.org/{"tv" if kind in {"tv","show"} else "movie"}/{tmdb}')
        result.update({'provider':'moviebase','catalog_provider':'moviebase','catalog_url':url,'source_url':url,'identity_resolution':'moviebase_tmdb_path_exact'})
        if requested_season is not None:
            result['selected_season'] = requested_season
        if requested_episode is not None:
            result['selected_episode'] = requested_episode
        return result
    title = str((query.get('title') or [''])[0]).strip()
    year = _year((query.get('year') or [''])[0])
    if not title:
        slug = parsed.path.rstrip('/').split('/')[-1]
        title = re.sub(r'-(?:19|20)\d{2}$', '', slug).replace('-', ' ')
        year = year or _year(slug)
    result = _tmdb_public_search(title, year, 'tv' if kind in {'tv', 'show'} else 'movie') or {}
    result.update({'provider': 'moviebase', 'catalog_provider': 'moviebase', 'catalog_url': url})
    result.setdefault('source_url', url)
    result.setdefault('media_type', 'tv' if kind in {'tv', 'show'} else 'movie')
    result.setdefault('title', title or 'Без назви')
    result.setdefault('original_title', title)
    result.setdefault('year', year)
    result.setdefault('poster', None)
    result.setdefault('overview', '')
    return result


def metadata(url: str) -> dict[str, Any]:
    host = _host(url)
    if 'themoviedb.org' in host:
        result = _tmdb_detail(url)
        result['catalog_url'] = url
        result['catalog_provider'] = 'tmdb'
    elif 'imdb.com' in host:
        result = _imdb_metadata(url)
    elif 'trakt.tv' in host:
        result = _trakt_metadata(url)
    elif 'moviebase.app' in host:
        result = _moviebase_metadata(url)
    else:
        raise ValueError('Непідтримуваний каталог.')
    result = _ensure_uk_overview(result)
    canonical_uid = _canonical_alias_uid(result, url)
    if canonical_uid:
        result['media_uid'] = canonical_uid
    else:
        if result.get('tmdb_id') or result.get('imdb_id'):
            uid_seed = f"{result.get('tmdb_id') or ''}|{result.get('imdb_id') or ''}|{result.get('media_type')}|{result.get('title')}|{result.get('year')}"
        else:
            uid_seed = f"{result.get('provider') or result.get('catalog_provider') or 'catalog'}|{result.get('source_url') or url}|{result.get('media_type') or 'unknown'}"
        result['media_uid'] = 'MED-' + hashlib.sha256(uid_seed.encode()).hexdigest()[:16].upper()
    return result


def _candidate_type(url: str) -> str:
    path = urlparse(url).path.lower()
    tv_tokens = (
        '/seriesss/', '/anime-series/', '/cartoonseries/', '/tv-series/',
        '/serials/', '/serial/', '/serialy/', '/multserialy/', '/multserial/',
    )
    if any(token in path for token in tv_tokens) or 'season' in path:
        return 'tv'
    return 'movie'


_IDENTITY_CYRILLIC_LATIN = str.maketrans({
    'а':'a','б':'b','в':'v','г':'g','ґ':'g','д':'d','е':'e','є':'e','ж':'zh','з':'z','и':'i','і':'i','ї':'i','й':'i',
    'к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts','ч':'ch',
    'ш':'sh','щ':'shch','ь':'','ю':'yu','я':'ya','ы':'y','э':'e','ё':'e','ъ':'',
})

def _identity_tokens(value: str) -> set[str]:
    raw = _normal(str(value or ''))
    raw = re.sub(r'\b(?:19|20)\d{2}\b', ' ', raw)
    raw = re.sub(r'\b\d+\s*(?:season|sezon|сезон)\b', ' ', raw)
    tokens: set[str] = set()
    for token in raw.split():
        if not token or token.isdigit():
            continue
        tokens.add(token)
        tokens.add(token.translate(_IDENTITY_CYRILLIC_LATIN))
        tokens.add(token.translate(_CYRILLIC_LATIN) if '_CYRILLIC_LATIN' in globals() else token)
    return {x for x in tokens if x and x not in _HISTORY_GENERIC} if '_HISTORY_GENERIC' in globals() else {x for x in tokens if x}

def _localized_single_token_match(meta: dict[str, Any], candidate_title: str) -> bool:
    target_values = [meta.get('title'), meta.get('original_title'), *(meta.get('search_aliases') or [])]
    target_tokens: set[str] = set()
    for value in target_values:
        target_tokens.update(_identity_tokens(str(value or '')))
    candidate_tokens = _identity_tokens(candidate_title)
    if not target_tokens or not candidate_tokens:
        return False
    for target in target_tokens:
        for candidate in candidate_tokens:
            if target == candidate:
                return True
            if min(len(target), len(candidate)) >= 5 and difflib.SequenceMatcher(None, target, candidate).ratio() >= 0.84:
                return True
    return False


def _proper_title_prefix(left_key: str, right_key: str) -> bool:
    left = [token for token in str(left_key or '').split() if token]
    right = [token for token in str(right_key or '').split() if token]
    if len(left) < 2 or len(right) < 2 or left == right:
        return False
    return (len(left) < len(right) and right[:len(left)] == left) or (len(right) < len(left) and left[:len(right)] == right)


def _strong_title_identity(meta: dict[str, Any], candidate_title: str) -> bool:
    candidate_key = _history_title_key(candidate_title)
    targets = {_history_title_key(meta.get('title', '')), _history_title_key(meta.get('original_title', ''))}
    targets.update(_history_title_key(value) for value in (meta.get('search_aliases') or []))
    targets.discard('')
    if not candidate_key or not targets:
        return False
    if candidate_key in targets:
        return True
    # Alternate-title pages commonly prefix the canonical title, e.g.
    # 'Забраковані / Кульгаві коні'. Treat a complete multi-token canonical
    # title embedded in the candidate as strong identity.
    padded_candidate = f' {candidate_key} '
    if any(len(target.split()) >= 2 and f' {target} ' in padded_candidate for target in targets):
        return True
    scores = [difflib.SequenceMatcher(None, candidate_key, target).ratio() for target in targets if not _proper_title_prefix(target, candidate_key)]
    if max(scores, default=0.0) >= 0.88:
        return True
    if any(len(target.split()) == 1 for target in targets):
        return _localized_single_token_match(meta, candidate_title)
    return False


def _candidate_content_compatible(meta: dict[str, Any], item: dict[str, Any]) -> bool:
    """Fail closed on identity contradictions, not merely search-title similarity.

    A TV pilot/season page may differ by one year from the canonical series
    premiere (pilot or territory date), while later season years remain valid.
    Movies keep the existing one-year festival/territory tolerance. For short
    one-token localized TV titles, require that token as a full title token so
    names such as "Річер" cannot match "Скрічери" and "Простір" cannot
    match "Кіберпростір".
    """
    page_url = str(item.get('page_url') or '')
    path = urlparse(page_url).path.lower()
    target_type = str(meta.get('media_type') or '')
    candidate_type = str(item.get('media_type') or (_candidate_type(page_url) if page_url else ''))
    content_type = str(meta.get('content_type') or '')
    if target_type == 'tv' and (candidate_type == 'movie' or '/filmy/' in path):
        return False
    if target_type == 'movie' and (candidate_type == 'tv' or any(token in path for token in ('/seriesss/', '/anime-series/', '/cartoonseries/', '/tv-series/'))):
        return False
    if content_type == 'animation_series' and any(token in path for token in ('/seriesss/drama_series/', '/filmy/')):
        return False
    try:
        target_year = int(meta.get('year') or 0)
    except (TypeError, ValueError):
        target_year = 0
    try:
        candidate_year = int(item.get('year') or 0)
    except (TypeError, ValueError):
        candidate_year = 0
    if target_year and candidate_year:
        if target_type == 'movie' and abs(candidate_year - target_year) > 1:
            return False
        if target_type == 'tv':
            if candidate_year < target_year - 1:
                return False
            # Later season pages may be newer than the premiere only when the
            # title still identifies the same work. A franchise prefix alone
            # must never absorb a spin-off.
            if candidate_year > target_year + 1 and not _strong_title_identity(meta, str(item.get('title') or '')):
                return False
    if target_type == 'tv':
        target_key = _history_title_key(meta.get('title', ''))
        candidate_key = _history_title_key(item.get('title', ''))
        identity_keys = {target_key, _history_title_key(meta.get('original_title', ''))}
        identity_keys.update(_history_title_key(value) for value in (meta.get('search_aliases') or []))
        identity_keys.discard('')
        if candidate_key and any(_proper_title_prefix(key, candidate_key) for key in identity_keys):
            return False
        target_tokens = target_key.split()
        candidate_tokens = set(candidate_key.split())
        if len(target_tokens) == 1 and candidate_key and target_tokens[0] not in candidate_tokens:
            if not _localized_single_token_match(meta, str(item.get('title') or '')):
                return False
    return True


def _source_candidate_for_identity(source: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    fallback = fallback if isinstance(fallback, dict) else {}
    page_url = str(
        source.get('discovery_page_url') or source.get('page_url') or
        source.get('origin_url') or fallback.get('page_url') or ''
    )
    title = str(
        source.get('discovery_page_title') or source.get('page_title') or
        fallback.get('title') or ''
    )
    page_title = str(source.get('page_title') or title)
    return {
        'title': title,
        'year': _year(page_title) or int(fallback.get('year') or 0),
        'media_type': _candidate_type(page_url) if page_url else str(fallback.get('media_type') or ''),
        'site': _host(page_url) if page_url else str(fallback.get('site') or ''),
        'page_url': page_url,
    }


def _candidate_score(meta: dict[str, Any], title: str, year: int, media_type: str) -> float:
    targets = [_normal(meta.get('title', '')), _normal(meta.get('original_title', ''))]
    targets.extend(_normal(value) for value in (meta.get('search_aliases') or []) if value)
    targets = list(dict.fromkeys(value for value in targets if value))
    candidate = _normal(title)
    similarity = max((difflib.SequenceMatcher(None, target, candidate).ratio() for target in targets if target), default=0.0)
    score = similarity * 0.55
    if candidate and candidate in targets:
        score += 0.20
    target_year = int(meta.get('year') or 0)
    if target_year and year == target_year:
        score += 0.35
    elif target_year and year:
        score -= min(0.30, abs(target_year - year) * 0.06)
    target_type = str(meta.get('media_type') or '')
    if target_type and media_type == target_type:
        score += 0.12
    elif target_type not in {'', 'unknown'}:
        score -= 0.15
    return round(score, 4)


_CYRILLIC_LATIN = str.maketrans({
    'а':'a','б':'b','в':'v','г':'h','ґ':'g','д':'d','е':'e','є':'ie','ж':'zh','з':'z','и':'y','і':'i','ї':'i','й':'i',
    'к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'kh','ц':'ts','ч':'ch',
    'ш':'sh','щ':'shch','ь':'','ю':'iu','я':'ia','ы':'y','э':'e','ё':'e','ъ':'',
})
_HISTORY_GENERIC = {
    'serial','series','show','tv','film','movie','detective','season','sezon','online','watch',
    'dyvytysia','ukrainskoiu','movoiu','vysokii','iakosti','hd','yakosti',
}


def _history_title_key(value: str) -> str:
    text = str(value or '').strip()
    text = re.sub(r'^\s*(?:мультсеріал|мультфільм|серіал|фільм|аніме|serial|series|film|movie|anime|cartoon)\s+', '', text, flags=re.I)
    text = re.split(r'\s*\((?:19|20)\d{2}\)|\s*\(?\d+\s*[-–]\s*\d+\s*(?:сезон|season|sezon)\)?|\b\d+\s*(?:сезон|season|sezon)\b|\b(?:дивитися|смотреть|watch)\b', text, maxsplit=1, flags=re.I)[0]
    text = text.strip()
    text = re.sub(r'\s+(?:аніме\s+)?(?:українською(?:\s+мовою)?|украинском(?:\s+языке)?)$', '', text, flags=re.I)
    text = _normal(text).translate(_CYRILLIC_LATIN)
    tokens = [token for token in text.split() if token and token not in _HISTORY_GENERIC and not token.isdigit()]
    return ' '.join(tokens)


def _catalog_payload(meta: dict[str, Any]) -> dict[str, Any]:
    keys = (
        'media_uid','catalog_provider','provider','catalog_url','source_url','media_type','title',
        'original_title','year','tmdb_id','imdb_id','poster','overview','overview_source',
        'content_type','season_posters','seasons','metadata_transport',
    )
    return {key: meta.get(key) for key in keys if meta.get(key) not in (None, '')}


def _base_discovery_result(meta: dict[str, Any], catalog_url: str, status: str, *, message: str = '', diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        'status': status,
        'title': str(meta.get('title') or 'Без назви'),
        'poster': meta.get('poster'),
        'overview': str(meta.get('overview') or ''),
        'catalog_url': catalog_url,
        'catalog': _catalog_payload(meta),
        'history_media_type': str(meta.get('media_type') or ''),
        'sources': [],
        'source_state': 'no_source',
        'trailer_only': False,
        'trailer_source_count': 0,
        'full_source_count': 0,
        'discovery': {
            'state': status,
            'message': message,
            **(diagnostics or {}),
        },
    }


_UK_AUDIO_MARKERS = (
    'українською','українська','український','українське','ukrainian','ukr',
    'dniprofilm','le-doyen','так треба','цікава ідея','postmodern','постмодерн',
    '1+1','плюс плюс','новий канал','ictv','мегого','megogo','sweet.tv','kyivstar',
)
_RU_AUDIO_MARKERS = (
    'русский','русская','русское','русская озвучка','рус.','російською','russian',
    'lostfilm','newstudio','кубик в кубе','hdrezka studio','red head sound',
)

def source_audio_language(source: dict[str, Any] | None) -> str:
    if not isinstance(source, dict):
        return 'unknown'
    explicit = str(source.get('audio_language') or '').strip().lower()
    if explicit in {'uk','ukr','ua'}:
        return 'uk'
    if explicit in {'ru','rus'}:
        return 'ru'
    if explicit in {'en','eng'}:
        return 'en'
    if explicit in {'de','deu','ger'}:
        return 'de'
    text = ' '.join(str(source.get(key) or '') for key in (
        'group','translation','title','page_title','discovery_page_title'
    )).casefold()
    # Explicit Russian studio/language labels beat incidental Ukrainian UI words.
    if any(marker in text for marker in _RU_AUDIO_MARKERS) or any(ch in text for ch in ('ы','э','ъ','ё')):
        return 'ru'
    if any(marker in text for marker in _UK_AUDIO_MARKERS) or any(ch in text for ch in ('і','ї','є','ґ')):
        return 'uk'
    return 'unknown'

def source_audio_priority(source: dict[str, Any] | None) -> int:
    lang=source_audio_language(source); pref=preferred_content_language(); order=[pref]+[x for x in ('uk','ru','en','de','unknown') if x!=pref]; return order.index(lang) if lang in order else len(order)

def _looks_ukrainian(value: str) -> bool:
    text = str(value or '').casefold()
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    cyr = sum('\u0400' <= ch <= '\u04ff' for ch in letters)
    if cyr / len(letters) < 0.70:
        return False
    if any(ch in text for ch in ('і','ї','є','ґ')):
        return True
    # Russian-only orthography is a clear negative. Ambiguous Cyrillic is sent
    # through the local translator so descriptions remain Ukrainian.
    return not any(ch in text for ch in ('ы','э','ъ','ё')) and any(token in text for token in (' та ',' що ',' як ',' його ',' її ',' для ',' від ',' цей ',' ця ',' це '))

def _ensure_uk_overview(meta: dict[str, Any]) -> dict[str, Any]:
    overview = str(meta.get('overview') or '').strip()
    if not overview or _looks_ukrainian(overview):
        return meta
    source_language = 'ru' if any(ch in overview.casefold() for ch in ('ы','э','ъ','ё')) else 'auto'
    translated = _local_translate_uk(overview, source_language)
    if translated:
        meta['overview'] = translated
        meta['overview_source'] = 'skeleton_translation_gateway_uk'
    return meta

_TRAILER_RE = re.compile(r'(^|[^a-zа-яіїєґ])(trailer|трейлер(?:и|ів|ом|а)?)([^a-zа-яіїєґ]|$)', re.I)

def is_trailer_source(source: dict[str, Any] | None) -> bool:
    if not isinstance(source, dict):
        return False
    if bool(source.get('is_trailer')) or str(source.get('source_type') or '').casefold() == 'trailer':
        return True
    marker = ' '.join(str(source.get(key) or '') for key in (
        'title','page_title','discovery_page_title','group','translation','episode','source_type','kind'
    )).casefold()
    return bool(_TRAILER_RE.search(marker))

def _decorate_source_types(result: dict[str, Any], media_type: str = '') -> dict[str, Any]:
    sources = result.get('sources') if isinstance(result.get('sources'), list) else []
    trailer_count = 0
    for source in sources:
        if not isinstance(source, dict):
            continue
        trailer = is_trailer_source(source)
        source['is_trailer'] = bool(trailer)
        source['source_type'] = 'trailer' if trailer else 'full_release'
        if trailer:
            trailer_count += 1
            # A trailer is metadata/fallback content, never an episode of a TV season.
            # Keeping provider-inferred season numbers here caused a DVD trailer to
            # masquerade as loaded S8 and polluted episode selectors.
            source['episode'] = 'Трейлер'
            source['season'] = ''
    full_count = max(0, len(sources) - trailer_count)
    state = 'no_source' if not sources else ('trailer_only' if full_count == 0 else 'full_release')
    result['source_state'] = state
    result['trailer_only'] = state == 'trailer_only'
    result['trailer_source_count'] = trailer_count
    result['full_source_count'] = full_count
    return result

def _meta_matches_job(meta: dict[str, Any], job: dict[str, Any]) -> bool:
    catalog = job.get('catalog') if isinstance(job.get('catalog'), dict) else {}
    mt = int(meta.get('tmdb_id') or 0)
    jt = int(catalog.get('tmdb_id') or 0)
    if mt and jt:
        return mt == jt and str(meta.get('media_type') or '') == str(catalog.get('media_type') or job.get('history_media_type') or '')
    mi = str(meta.get('imdb_id') or '')
    ji = str(catalog.get('imdb_id') or '')
    if mi and ji:
        return mi == ji
    mu = str(meta.get('media_uid') or '')
    ju = str(catalog.get('media_uid') or '')
    if mu and ju:
        return mu == ju
    target = _history_title_key(meta.get('title', ''))
    candidate = _history_title_key(catalog.get('title') or job.get('title') or '')
    if not target or target != candidate:
        return False
    my = int(meta.get('year') or 0)
    jy = int(catalog.get('year') or _year(job.get('title')) or 0)
    return not my or not jy or my == jy


def _candidate_from_job(meta: dict[str, Any], path: Path, job: dict[str, Any]) -> dict[str, Any] | None:
    if job.get('status') != 'ready' or not job.get('sources') or not _meta_matches_job(meta, job):
        return None
    page_url = str(job.get('page_url') or '')
    if not page_url or is_catalog_url(page_url):
        return None
    discovery = job.get('discovery') if isinstance(job.get('discovery'), dict) else {}
    selected = discovery.get('selected') if isinstance(discovery.get('selected'), dict) else {}
    source = next((item for item in job.get('sources') or [] if str(item.get('discovery_page_url') or item.get('history_page_url') or '') == page_url), None) or {}
    actual_title = str(selected.get('title') or source.get('discovery_page_title') or source.get('page_title') or '').strip()
    if not actual_title:
        return None
    actual_year = int(selected.get('year') or _year(str(source.get('page_title') or '')) or 0)
    item = {
        'title': actual_title, 'year': actual_year,
        'media_type': str(selected.get('media_type') or _candidate_type(page_url)),
        'site': _host(page_url), 'page_url': page_url, 'score': 1.55,
        'history_exact': True, 'cache_verified': True,
        'history_job_id': str(job.get('job_id') or path.stem),
        'history_mtime': path.stat().st_mtime,
    }
    return item if _candidate_content_compatible(meta, item) else None


def _cached_verified_candidates(meta: dict[str, Any]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    jobs = STATE / 'jobs'
    for path in sorted(jobs.glob('*.json'), key=lambda value: value.stat().st_mtime, reverse=True):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        item = _candidate_from_job(meta, path, job)
        if not item:
            continue
        previous = found.get(item['page_url'])
        if previous is None or item['history_mtime'] > previous.get('history_mtime', 0):
            found[item['page_url']] = item
    try:
        with _db() as con:
            rows = con.execute(
                "SELECT selected_page_url,selected_score,updated_at,details_json FROM discoveries WHERE media_uid=? AND status='ready' AND selected_page_url IS NOT NULL ORDER BY updated_at DESC LIMIT 12",
                (str(meta.get('media_uid') or ''),),
            ).fetchall()
        for row in rows:
            page_url = str(row['selected_page_url'] or '')
            if not page_url or page_url in found:
                continue
            try:
                details = json.loads(str(row['details_json'] or '{}'))
            except Exception:
                details = {}
            candidates = details.get('candidates') if isinstance(details.get('candidates'), list) else []
            selected = next((item for item in candidates if isinstance(item, dict) and str(item.get('page_url') or '') == page_url), None)
            if not selected:
                continue
            item = {
                'title': str(selected.get('title') or ''), 'year': int(selected.get('year') or 0),
                'media_type': str(selected.get('media_type') or _candidate_type(page_url)),
                'site': _host(page_url), 'page_url': page_url,
                'score': max(1.30, float(row['selected_score'] or 0)),
                'cache_verified': True, 'discovery_cache': True,
            }
            if _candidate_content_compatible(meta, item):
                found[page_url] = item
    except Exception:
        pass
    return sorted(found.values(), key=lambda item: (bool(item.get('history_job_id')), float(item.get('score') or 0), float(item.get('history_mtime') or 0)), reverse=True)


def _recent_negative(meta: dict[str, Any]) -> dict[str, Any] | None:
    uid = str(meta.get('media_uid') or '')
    if not uid:
        return None
    try:
        with _db() as con:
            row = con.execute(
                "SELECT updated_at,details_json FROM discoveries WHERE media_uid=? AND status='no_verified_sources' ORDER BY updated_at DESC LIMIT 1",
                (uid,),
            ).fetchone()
        if not row:
            return None
        details = json.loads(str(row['details_json'] or '{}'))
        prior = ((details.get('metadata') or {}).get('_search_diagnostics') or {})
        if prior.get('version') != SEARCH_VERSION:
            return None
        updated = dt.datetime.fromisoformat(str(row['updated_at']).replace('Z','+00:00'))
        age = (dt.datetime.now(dt.timezone.utc) - updated).total_seconds()
        current_year = dt.datetime.now(dt.timezone.utc).year
        ongoing = str(meta.get('media_type') or '') == 'tv' and int(meta.get('year') or 0) >= current_year - 1
        ttl = 30 * 60 if ongoing else NEGATIVE_CACHE_SECONDS
        if age < ttl:
            return {'age_seconds': int(age), 'ttl_seconds': ttl, 'search_version': SEARCH_VERSION}
    except Exception:
        return None
    return None


def _decode_search_result_url(value: str) -> str:
    raw = str(value or '').strip()
    if raw.startswith('//'):
        raw = 'https:' + raw
    parsed = urlparse(raw)
    if parsed.hostname and parsed.hostname.endswith('duckduckgo.com'):
        target = str((parse_qs(parsed.query).get('uddg') or [''])[0])
        if target:
            return target
    return raw


BRAVE_MONTHLY_REQUEST_LIMIT = int(os.environ.get('BRAVE_MONTHLY_REQUEST_LIMIT') or '1000')
BRAVE_BUDGET_DB = STATE / 'brave-budget.sqlite3'

class BraveBudgetExceeded(RuntimeError):
    pass

def _brave_budget_reserve() -> None:
    month = time.strftime('%Y-%m', time.gmtime())
    con = sqlite3.connect(BRAVE_BUDGET_DB, timeout=10)
    try:
        con.execute('CREATE TABLE IF NOT EXISTS usage(month TEXT PRIMARY KEY, requests INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL)')
        con.execute('BEGIN IMMEDIATE')
        con.execute('INSERT OR IGNORE INTO usage(month,requests,updated_at) VALUES(?,?,?)',(month,0,int(time.time())))
        used = int(con.execute('SELECT requests FROM usage WHERE month=?',(month,)).fetchone()[0])
        if used >= BRAVE_MONTHLY_REQUEST_LIMIT:
            con.rollback(); raise BraveBudgetExceeded(f'Brave monthly free-credit budget exhausted: {used}/{BRAVE_MONTHLY_REQUEST_LIMIT}')
        con.execute('UPDATE usage SET requests=requests+1,updated_at=? WHERE month=?',(int(time.time()),month))
        con.commit()
    finally:
        con.close()

def _brave_get(*, headers: dict[str,str], params: dict[str,Any], timeout: int):
    _brave_budget_reserve()
    return requests.get(BRAVE_SEARCH_ENDPOINT, headers=headers, params=params, timeout=timeout)

def _brave_api_key() -> str:
    path = Path(os.environ.get('BRAVE_SEARCH_API_KEY_FILE') or BRAVE_KEY_FILE_DEFAULT)
    try:
        value = path.read_text(encoding='utf-8').strip()
    except OSError:
        return ''
    return value if len(value) >= 20 and not re.search(r'\s', value) else ''


def _brave_search(query: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
    key = _brave_api_key()
    if not query or not key:
        return []
    site_filter = ' OR '.join('site:' + domain for domain in SEARCH_DOMAINS)
    response = _brave_get(
        headers={
            'Accept': 'application/json',
            'Accept-Encoding': 'gzip',
            'X-Subscription-Token': key,
            'User-Agent': UA,
        },
        params={
            'q': f'"{query}" ({site_filter})',
            'country': 'DE',
            'count': 20,
            'safesearch': 'moderate',
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    rows: list[dict[str, Any]] = []
    for node in ((payload.get('web') or {}).get('results') or [])[:30]:
        title = ' '.join(str(node.get('title') or '').split())
        page_url = str(node.get('url') or '').strip()
        host = _host(page_url)
        if not title or not page_url or not any(host == domain or host.endswith('.' + domain) for domain in SEARCH_DOMAINS):
            continue
        context = title + ' ' + str(node.get('description') or '')
        year = _year(context)
        media_type = _candidate_type(page_url)
        rows.append({
            'title': title,
            'year': year,
            'media_type': media_type,
            'site': host,
            'page_url': page_url,
            'score': _candidate_score(meta, title, year, media_type),
            'search_provider': 'brave',
        })
    return rows


def _brave_domain_search(query: str, domain: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
    key = _brave_api_key()
    if not query or not domain or not key:
        return []
    response = _brave_get(
        headers={'Accept':'application/json','Accept-Encoding':'gzip','X-Subscription-Token':key,'User-Agent':UA},
        params={'q': f'"{query}" site:{domain}','country':'DE','count':10,'safesearch':'moderate'},
        timeout=12,
    )
    response.raise_for_status()
    rows: list[dict[str, Any]] = []
    for node in ((response.json().get('web') or {}).get('results') or [])[:15]:
        title=' '.join(str(node.get('title') or '').split())
        page_url=str(node.get('url') or '').strip()
        host=_host(page_url)
        if not title or not page_url or not (host == domain or host.endswith('.'+domain)):
            continue
        context=title+' '+str(node.get('description') or '')
        year=_year(context)
        media_type=_candidate_type(page_url)
        rows.append({
            'title':title,'year':year,'media_type':media_type,'site':host,'page_url':page_url,
            'score':_candidate_score(meta,title,year,media_type),'search_provider':'brave_site',
        })
    return rows


def _availability_search(query: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
    key = _brave_api_key()
    if not query or not key:
        return []
    site_filter = ' OR '.join('site:' + domain for domain in AVAILABILITY_SEARCH_DOMAINS)
    response = _brave_get(
        headers={'Accept':'application/json','Accept-Encoding':'gzip','X-Subscription-Token':key,'User-Agent':UA},
        params={'q': f'"{query}" ({site_filter})','country':'DE','count':12,'safesearch':'moderate'},
        timeout=12,
    )
    response.raise_for_status()
    rows: list[dict[str, Any]] = []
    for node in ((response.json().get('web') or {}).get('results') or [])[:20]:
        title = ' '.join(str(node.get('title') or '').split())
        page_url = str(node.get('url') or '').strip()
        host = _host(page_url)
        if not title or not page_url or not any(host == d or host.endswith('.' + d) for d in AVAILABILITY_SEARCH_DOMAINS):
            continue
        context = title + ' ' + str(node.get('description') or '')
        year = _year(context)
        media_type = _candidate_type(page_url)
        score = _candidate_score(meta, title, year, media_type)
        if score < 0.45:
            continue
        rows.append({
            'title': title, 'year': year, 'media_type': media_type, 'site': host,
            'page_url': page_url, 'score': score, 'availability_only': True,
            'search_provider': 'brave_availability',
        })
    return rows


def _indexed_site_search(query: str, meta: dict[str, Any]) -> list[dict[str, Any]]:
    search_url = 'https://html.duckduckgo.com/html/?q=' + quote(f'"{query}" {int(meta.get("year") or 0) or ""} {content_search_suffix()}')
    response = requests.get(
        search_url,
        headers={'User-Agent': UA, 'Accept-Language': preferred_accept_language()},
        timeout=8,
        allow_redirects=True,
    )
    if response.status_code not in (200, 202):
        return []
    soup = BeautifulSoup(response.text, 'lxml')
    rows: list[dict[str, Any]] = []
    for anchor in soup.select('.result__a')[:30]:
        page_url = _decode_search_result_url(str(anchor.get('href') or ''))
        host = _host(page_url)
        if not any(host == domain or host.endswith('.' + domain) for domain in SEARCH_DOMAINS):
            continue
        title = ' '.join(anchor.get_text(' ', strip=True).split())
        context = ' '.join((anchor.find_parent(class_='result') or anchor.parent or anchor).get_text(' ', strip=True).split())
        year = _year(context)
        media_type = _candidate_type(page_url)
        rows.append({
            'title': title, 'year': year, 'media_type': media_type, 'site': host,
            'page_url': page_url, 'score': _candidate_score(meta, title, year, media_type),
            'search_provider': 'duckduckgo_html',
        })
    return rows


def _reuse_cached_job(candidate: dict[str, Any], meta: dict[str, Any], catalog_url: str) -> dict[str, Any] | None:
    job_id = str(candidate.get('history_job_id') or '')
    if not re.fullmatch(r'[0-9a-f]{16}', job_id):
        return None
    path = STATE / 'jobs' / f'{job_id}.json'
    try:
        cached = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    if cached.get('status') != 'ready' or not cached.get('sources'):
        return None
    cached_catalog = cached.get('catalog') if isinstance(cached.get('catalog'), dict) else {}
    target_has_external = bool(meta.get('tmdb_id') or meta.get('imdb_id'))
    cached_has_external = bool(cached_catalog.get('tmdb_id') or cached_catalog.get('imdb_id'))
    if target_has_external and (not cached_has_external or not _meta_matches_job(meta, cached)):
        return None
    result = json.loads(json.dumps(cached))
    result.pop('job_id', None)
    result.pop('history_id', None)
    result.pop('request_kind', None)
    reused_sources = json.loads(json.dumps(cached.get('sources') or []))
    result['sources'] = [
        source for source in reused_sources
        if _candidate_content_compatible(meta, _source_candidate_for_identity(source, candidate))
    ]
    if not result['sources']:
        return None
    return _decorate_discovered_result(result, meta, catalog_url, candidate)


def _resolve_candidate(candidate: dict[str, Any], meta: dict[str, Any], catalog_url: str, resolve: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    if not _candidate_content_compatible(meta, candidate):
        raise ValueError('candidate_identity_mismatch')
    cached = _reuse_cached_job(candidate, meta, catalog_url)
    if cached and cached.get('sources'):
        cached['discovery_reused_without_network'] = True
        return cached
    result = resolve(candidate['page_url'])
    sources = result.get('sources') if isinstance(result.get('sources'), list) else []
    result['sources'] = [
        source for source in sources
        if _candidate_content_compatible(meta, _source_candidate_for_identity(source, candidate))
    ]
    if not result['sources']:
        raise ValueError('resolved_sources_identity_mismatch')
    return _decorate_discovered_result(result, meta, catalog_url, candidate)


def _history_candidates(meta: dict[str, Any]) -> list[dict[str, Any]]:
    targets = {_history_title_key(meta.get('title', '')), _history_title_key(meta.get('original_title', ''))}
    targets.discard('')
    if not targets:
        return []
    jobs = STATE / 'jobs'
    found: dict[str, dict[str, Any]] = {}
    for path in jobs.glob('*.json'):
        try:
            job = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if job.get('status') != 'ready' or not isinstance(job.get('sources'), list) or not job.get('sources'):
            continue
        page_url = str(job.get('page_url') or '')
        if not page_url or is_catalog_url(page_url):
            continue
        title = str(job.get('title') or '')
        key = _history_title_key(title)
        if not key:
            continue
        ratio = max((difflib.SequenceMatcher(None, target, key).ratio() for target in targets), default=0.0)
        exact = key in targets
        if not exact and ratio < 0.88:
            continue
        item = {
            'title': title,
            'year': _year(title),
            'media_type': _candidate_type(page_url),
            'site': _host(page_url),
            'page_url': page_url,
            'score': 1.25 if exact else round(0.82 + ratio * 0.18, 4),
            'history_exact': exact,
            'history_job_id': str(job.get('job_id') or path.stem),
            'history_mtime': path.stat().st_mtime,
        }
        previous = found.get(page_url)
        if previous is None or (item['score'], item['history_mtime']) > (previous['score'], previous['history_mtime']):
            found[page_url] = item
    return sorted(found.values(), key=lambda item: (bool(item.get('history_exact')), item['score'], item.get('history_mtime', 0)), reverse=True)


def _parse_uakino_search(text: str, base_url: str, meta: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    soup = BeautifulSoup(text, 'lxml')
    rows: list[dict[str, Any]] = []
    for item in soup.select('.movie-item'):
        anchor = item.find('a', href=True)
        title_el = item.select_one('.movie-title') or item.select_one('.deck-title')
        if not anchor or not title_el:
            continue
        page_url = urljoin(base_url, str(anchor['href']))
        host = _host(page_url)
        if host != 'uakino.best' or '/news/' in urlparse(page_url).path:
            continue
        title = ' '.join(title_el.get_text(' ', strip=True).split())
        blob = ' '.join(item.get_text(' ', strip=True).split())
        year_match = re.search(r'Рік виходу:\s*((?:19|20)\d{2})', blob)
        year = int(year_match.group(1)) if year_match else 0
        media_type = _candidate_type(page_url)
        score = _candidate_score(meta, title, year, media_type)
        canonical_key = _history_title_key(str(meta.get('title') or ''))
        candidate_key = _history_title_key(title)
        if len(canonical_key.split()) >= 2 and f' {canonical_key} ' in f' {candidate_key} ':
            score = max(score, 0.86)
        rows.append({
            'title': title, 'year': year, 'media_type': media_type,
            'site': host, 'page_url': page_url,
            'score': score, 'search_provider': 'uakino_native',
        })
    max_page = 1
    navigation = soup.select_one('.navigation')
    if navigation:
        for anchor in navigation.find_all('a'):
            match = re.search(r'formNavigation\((\d+)\)', str(anchor.get('onclick') or ''))
            if match:
                max_page = max(max_page, int(match.group(1)))
        for value in re.findall(r'\b\d+\b', navigation.get_text(' ', strip=True)):
            max_page = max(max_page, int(value))
    return rows, max_page


def _chrome_uakino_search_page(query: str, page: int, *, timeout: int = 100) -> str:
    body = (
        '<!doctype html><meta charset="utf-8">'
        '<form id="f" action="https://uakino.best/index.php?do=search" method="post">'
        '<input name="do" value="search"><input name="subaction" value="search">'
        f'<input name="from_page" value="{max(1, int(page))}">'
        f'<input name="story" value="{html.escape(str(query).casefold(), quote=True)}">'
        '</form><script>setTimeout(()=>document.getElementById("f").submit(),150)</script>'
    )
    bootstrap = 'data:text/html;charset=utf-8,' + quote(body, safe='')
    diagnostics: list[str] = []
    for attempt in range(3):
        profile = tempfile.mkdtemp(prefix='skeleton-cast-search-post-', dir='/tmp')
        try:
            profile_path = Path(profile)
            (profile_path / 'Default').mkdir(parents=True, exist_ok=True)
            (profile_path / 'First Run').touch()
            command = [
                '/usr/bin/google-chrome', '--headless=new', '--no-sandbox', '--disable-dev-shm-usage',
                '--disable-gpu', '--disable-blink-features=AutomationControlled', '--disable-background-networking',
                '--disable-component-update', '--disable-default-apps', '--disable-extensions', '--disable-sync',
                '--disable-translate', '--metrics-recording-only', '--mute-audio', '--no-first-run',
                '--no-default-browser-check', '--disable-breakpad', '--crash-dumps-dir=/dev/null',
                '--disable-features=MediaRouter,GlobalMediaControls,OptimizationHints,PushMessaging,NotificationTriggers,InterestFeedContentSuggestions',
                '--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
                '--lang=uk-UA', f'--user-data-dir={profile}', '--virtual-time-budget=60000', '--dump-dom', bootstrap,
            ]
            chrome_env = os.environ.copy()
            chrome_env.update({'HOME': profile, 'XDG_CONFIG_HOME': str(profile_path / 'config'), 'XDG_CACHE_HOME': str(profile_path / 'cache')})
            process = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False, env=chrome_env)
            text = process.stdout or ''
            lowered = text.casefold()
            if process.returncode == 0 and len(text) > 1000 and 'movie-item' in lowered and not any(marker in lowered for marker in ('cf-chl','just a moment','checking your browser')):
                return text
            diagnostics.append(f'attempt={attempt + 1} rc={process.returncode} html={len(text)}')
        except subprocess.TimeoutExpired:
            diagnostics.append(f'attempt={attempt + 1} timeout')
        finally:
            shutil.rmtree(profile, ignore_errors=True)
    raise RuntimeError(f'uakino_search_page_{page}: ' + '; '.join(diagnostics))


def _uakino_search(query: str, meta: dict[str, Any], *, paginate: bool = False) -> list[dict[str, Any]]:
    url = 'https://uakino.best/index.php?do=search&subaction=search&story=' + quote(query)
    text = resolver._chrome_text(url, timeout=UAKINO_SEARCH_TIMEOUT)
    rows, max_page = _parse_uakino_search(text, url, meta)
    found: dict[str, dict[str, Any]] = {item['page_url']: item for item in rows}
    if paginate:
        for page in range(2, min(max_page, 6) + 1):
            try:
                page_text = _chrome_uakino_search_page(query, page)
                page_rows, _ = _parse_uakino_search(page_text, url, meta)
                for item in page_rows:
                    previous = found.get(item['page_url'])
                    if previous is None or item['score'] > previous['score']:
                        found[item['page_url']] = item
            except Exception:
                continue
    return list(found.values())


def _search_aliases(meta: dict[str, Any]) -> list[str]:
    aliases: list[str] = []

    def add(value: Any) -> None:
        clean = _catalog_search_title(str(value or ''))
        if clean and clean.casefold() not in {item.casefold() for item in aliases}:
            aliases.append(clean)

    add(meta.get('title'))
    for existing_alias in (meta.get('search_aliases') or []):
        add(existing_alias)
    catalog_url = str(meta.get('catalog_url') or meta.get('source_url') or '')
    trakt = _trakt_external_id(catalog_url)
    if trakt:
        slug = trakt[0].split('/', 1)[1]
        add(re.sub(r'-(?:19|20)\d{2}$', '', slug).replace('-', ' '))
    tmdb_id = int(meta.get('tmdb_id') or 0)
    media_type = str(meta.get('media_type') or '')
    if tmdb_id and media_type in {'tv','movie'}:
        for locale in SEARCH_ALIAS_LOCALES:
            item = _proxy('/v1/item', {'media_type': media_type, 'tmdb_id': tmdb_id, 'language': locale}) or {}
            add(item.get('title'))
            add(item.get('original_title'))
    add(meta.get('original_title'))

    for value in list(aliases):
        for part in re.findall(r'[A-Za-z][A-Za-z0-9 :_\-]{3,}', value):
            add(part.strip(' :_-'))
        if re.search(r'[А-Яа-яІіЇїЄєҐґ]', value):
            add(re.sub(r'\bу\s+(?=[А-Яа-яІіЇїЄєҐґ])', 'в ', value, flags=re.I))
            add(re.sub(r'\bв\s+(?=[А-Яа-яІіЇїЄєҐґ])', 'у ', value, flags=re.I))
    meta['search_aliases'] = aliases
    return aliases


def _catalog_search_title(value: str) -> str:
    text = str(value or '').strip()
    text = re.sub(r'^\s*(?:мультсеріал|мультфільм|серіал|фільм|аніме|serial|series|film|movie|anime|cartoon)\s+', '', text, flags=re.I)
    text = re.split(r'\s*\((?:19|20)\d{2}\)|\s*\(?\d+\s*[-–]\s*\d+\s*(?:сезон|season|sezon)\)?|\b\d+\s*(?:сезон|season|sezon)\b|\b(?:дивитися|смотреть|watch)\b', text, maxsplit=1, flags=re.I)[0]
    text = text.strip()
    text = re.sub(r'\s+(?:аніме\s+)?(?:українською(?:\s+мовою)?|украинском(?:\s+языке)?)$', '', text, flags=re.I)
    return re.sub(r'\s+', ' ', text).strip()


def candidates(meta: dict[str, Any], *, force: bool = False) -> list[dict[str, Any]]:
    diagnostics: dict[str, Any] = {'version': SEARCH_VERSION, 'cache': False, 'queries': [], 'errors': []}
    cached = [item for item in _cached_verified_candidates(meta) if _candidate_content_compatible(meta, item)]
    if cached and not force:
        diagnostics['cache'] = True
        diagnostics['candidate_count'] = len(cached)
        meta['_search_diagnostics'] = diagnostics
        return cached[:60]
    if force:
        diagnostics['forced_fresh_search'] = True

    history = [item for item in _history_candidates(meta) if _candidate_content_compatible(meta, item)]
    exact_history = [item for item in history if item.get('history_exact') and item.get('history_job_id')]
    if exact_history and not force:
        diagnostics['cache'] = True
        diagnostics['history_exact'] = True
        diagnostics['candidate_count'] = len(exact_history)
        meta['_search_diagnostics'] = diagnostics
        return exact_history[:60]

    queries = _search_aliases(meta)
    diagnostics['aliases'] = queries[:12]
    found: dict[str, dict[str, Any]] = {item['page_url']: item for item in history}

    def plausible() -> bool:
        return any(float(item.get('score') or 0) >= 0.45 for item in found.values())

    # Brave is the fast indexed path. The browser-driven site search is now
    # a bounded fallback because it can stall on Cloudflare or page rendering.
    if (force or not plausible()) and _brave_api_key():
        for query in queries[:6]:
            diagnostics['queries'].append({'provider': 'brave', 'query': query})
            try:
                for item in _brave_search(query, meta):
                    previous = found.get(item['page_url'])
                    if previous is None or item['score'] > previous['score']:
                        found[item['page_url']] = item
            except Exception as exc:
                diagnostics['errors'].append(f'brave:{type(exc).__name__}')
            if plausible() and not force:
                break

    # Dedicated provider probes prevent a dominant indexed site from consuming
    # all top Brave results. These are bounded to the first two identity aliases.
    if _brave_api_key():
        for query in queries[:2]:
            for domain in DEDICATED_PLAYABLE_SEARCH_DOMAINS:
                diagnostics['queries'].append({'provider': 'brave_site', 'domain': domain, 'query': query})
                try:
                    for item in _brave_domain_search(query, domain, meta):
                        previous=found.get(item['page_url'])
                        if previous is None or item['score'] > previous['score']:
                            found[item['page_url']]=item
                except Exception as exc:
                    diagnostics['errors'].append(f'brave_site:{domain}:{type(exc).__name__}')

    # UAKino is a first-class playable provider, not only a fallback. Brave can
    # return plausible results from other sites and previously suppressed this
    # provider entirely. Always ensure at least one bounded UAKino probe ran.
    def has_uakino_candidate() -> bool:
        return any((urlparse(str(item.get('page_url') or '')).hostname or '').lower() == 'uakino.best' for item in found.values())

    for query in queries[:2]:
        diagnostics['queries'].append({'provider': 'uakino_direct', 'query': query})
        try:
            for item in _uakino_search(query, meta, paginate=False):
                previous = found.get(item['page_url'])
                if previous is None or item['score'] > previous['score']:
                    found[item['page_url']] = item
        except Exception as exc:
            diagnostics['errors'].append(f'uakino:{type(exc).__name__}')
        if any((urlparse(str(item.get('page_url') or '')).hostname or '').lower() == 'uakino.best' and float(item.get('score') or 0) >= 0.75 for item in found.values()):
            break

    if not plausible():
        for query in queries[:4]:
            diagnostics['queries'].append({'provider': 'duckduckgo_fallback', 'query': query})
            try:
                for item in _indexed_site_search(query, meta):
                    previous = found.get(item['page_url'])
                    if previous is None or item['score'] > previous['score']:
                        found[item['page_url']] = item
            except Exception as exc:
                diagnostics['errors'].append(f'duckduckgo:{type(exc).__name__}')
            if plausible():
                break

    availability: dict[str, dict[str, Any]] = {}
    if _brave_api_key():
        for query in queries[:3]:
            diagnostics['queries'].append({'provider': 'brave_availability', 'query': query})
            try:
                for item in _availability_search(query, meta):
                    previous = availability.get(item['page_url'])
                    if previous is None or item['score'] > previous['score']:
                        availability[item['page_url']] = item
            except Exception as exc:
                diagnostics['errors'].append(f'availability:{type(exc).__name__}')
    if availability:
        diagnostics['availability_candidates'] = [
            {key: item.get(key) for key in ('title','year','media_type','site','score','page_url') if item.get(key) not in (None,'')}
            for item in sorted(availability.values(), key=lambda x: float(x.get('score') or 0), reverse=True)[:12]
        ]

    incompatible = [item for item in found.values() if not _candidate_content_compatible(meta, item)]
    if incompatible:
        diagnostics['rejected_content_mismatch'] = [str(item.get('page_url') or '') for item in incompatible[:20]]
    compatible = [item for item in found.values() if _candidate_content_compatible(meta, item)]
    rows = sorted(compatible, key=lambda item: (
        bool(item.get('cache_verified')), bool(item.get('history_exact')),
        float(item.get('score') or 0), float(item.get('history_mtime') or 0), int(item.get('year') or 0),
    ), reverse=True)
    diagnostics['candidate_count'] = len(rows)
    meta['_search_diagnostics'] = diagnostics
    for item in rows:
        item['query_errors'] = diagnostics['errors'][:4]
    return rows[:60]


def season_candidates(meta: dict[str, Any]) -> list[dict[str, Any]]:
    # Search the work itself first, not `title + N season`. Providers such as
    # UAKino expose season pages from the base-title query while their native
    # search can return zero results for an otherwise exact season-qualified query.
    base_meta = dict(meta)
    base_meta.pop('season', None)
    base_meta.pop('selected_season', None)
    history = [item for item in _history_candidates(base_meta) if _candidate_content_compatible(base_meta, item)]
    found: dict[str, dict[str, Any]] = {item['page_url']: item for item in history}
    # Reuse the broad multi-provider search before the slower paginated provider
    # scan. This is where Brave/UAserial often exposes older season pages that
    # UAKino's own search hides after a newer season is published.
    try:
        for item in candidates(base_meta, force=True):
            if not _candidate_content_compatible(base_meta, item):
                continue
            previous = found.get(item['page_url'])
            if previous is None or float(item.get('score') or 0) > float(previous.get('score') or 0):
                found[item['page_url']] = dict(item)
    except Exception:
        pass
    # UAFix uses stable sibling season URLs. If search reveals any season page,
    # derive the other seasons up to the highest season seen across providers.
    # They are still verified by resolve_page before entering history.
    detected_seasons = [int(v) for v in (_candidate_season(item) for item in found.values()) if v]
    max_detected_season = max(detected_seasons, default=0)
    uafix_template = None
    for item in list(found.values()):
        page_url = str(item.get('page_url') or '')
        if _host(page_url) != 'uafix.net':
            continue
        match = re.search(r'(/sezon-)\d+(/?$)', urlparse(page_url).path, re.I)
        if match:
            uafix_template = (page_url[:page_url.find(match.group(0))] + match.group(1), match.group(2) or '/')
            break
    if uafix_template and max_detected_season > 0:
        prefix, suffix = uafix_template
        for season_no in range(1, min(max_detected_season, 20) + 1):
            page_url = f'{prefix}{season_no}{suffix}'
            if page_url in found:
                continue
            found[page_url] = {
                'title': f"{str(meta.get('title') or '')} ({season_no} сезон)",
                'year': 0, 'media_type': 'tv', 'site': 'uafix.net',
                'page_url': page_url, 'score': 0.78,
                'search_provider': 'uafix_sibling',
            }

    # UAFix uses stable sibling season URLs. If search reveals any season page,
    # derive the other seasons up to the highest season seen across providers.
    # They are still verified by resolve_page before entering history.
    detected_seasons = [int(v) for v in (_candidate_season(item) for item in found.values()) if v]
    max_detected_season = max(detected_seasons, default=0)
    uafix_template = None
    for item in list(found.values()):
        page_url = str(item.get('page_url') or '')
        if _host(page_url) != 'uafix.net':
            continue
        match = re.search(r'(/sezon-)\d+(/?$)', urlparse(page_url).path, re.I)
        if match:
            uafix_template = (page_url[:page_url.find(match.group(0))] + match.group(1), match.group(2) or '/')
            break
    if uafix_template and max_detected_season > 0:
        prefix, suffix = uafix_template
        for season_no in range(1, min(max_detected_season, 20) + 1):
            page_url = f'{prefix}{season_no}{suffix}'
            if page_url in found:
                continue
            found[page_url] = {
                'title': f"{str(meta.get('title') or '')} ({season_no} сезон)",
                'year': 0, 'media_type': 'tv', 'site': 'uafix.net',
                'page_url': page_url, 'score': 0.78,
                'search_provider': 'uafix_sibling',
            }

    queries: list[str] = []
    values = [meta.get('title'), meta.get('original_title')]
    localized_history = next((item.get('title') for item in history if re.search(r'[А-Яа-яІіЇїЄєҐґ]', str(item.get('title') or ''))), None)
    values.append(localized_history)
    for value in values:
        clean = _catalog_search_title(str(value or ''))
        if clean and clean.casefold() not in {item.casefold() for item in queries}:
            queries.append(clean)
    errors: list[str] = []
    for query in queries[:3]:
        try:
            for item in _uakino_search(query, meta, paginate=True):
                previous = found.get(item['page_url'])
                if previous is None or item['score'] > previous['score']:
                    found[item['page_url']] = item
        except Exception as exc:
            errors.append(f'{type(exc).__name__}: {exc}'[:300])

    tmdb_id = int(meta.get('tmdb_id') or 0)
    expected_seasons: list[int] = []
    if tmdb_id:
        empty_run = 0
        for season_no in range(1, 21):
            try:
                season_meta = tmdb_season_metadata(tmdb_id, season_no, language='en-US', force=False)
                count = int(season_meta.get('episode_count') or 0)
            except Exception:
                count = 0
            if count > 0:
                expected_seasons.append(season_no); empty_run = 0
            else:
                empty_run += 1
                if expected_seasons and empty_run >= 2:
                    break
    existing_numbers = {int(v) for v in (_candidate_season(x) for x in found.values()) if v}
    aliases = _search_aliases(meta)
    localized = next((a for a in aliases if re.search(r'[А-Яа-яІіЇїЄєҐґ]', a)), str(meta.get('title') or ''))
    original = next((a for a in aliases if re.search(r'[A-Za-z]', a)), str(meta.get('original_title') or ''))
    for season_no in [x for x in expected_seasons if x not in existing_numbers]:
        season_rows: list[dict[str, Any]] = []
        for domain in DEDICATED_PLAYABLE_SEARCH_DOMAINS:
            for alias in [localized, original]:
                if not alias:
                    continue
                try:
                    probe = _brave_domain_search(f'"{alias}" "{season_no} сезон"', domain, meta)
                except Exception as exc:
                    errors.append(f'season{season_no}:{domain}:{type(exc).__name__}'[:300]); continue
                for item in probe:
                    item = dict(item)
                    detected = _candidate_season(item)
                    if detected and detected != season_no:
                        continue
                    title = str(item.get('title') or '')
                    if not (_strong_title_identity(meta, title) or _localized_single_token_match(meta, title)):
                        continue
                    item['cross_search_season'] = str(season_no)
                    item['search_provider'] = 'season_backfill_exact'
                    item['score'] = max(float(item.get('score') or 0), 0.72)
                    season_rows.append(item)
        for item in season_rows:
            previous = found.get(item['page_url'])
            if previous is None or float(item.get('score') or 0) > float(previous.get('score') or 0):
                found[item['page_url']] = item
    rows = sorted((item for item in found.values() if _candidate_content_compatible(meta, item)), key=lambda item: (
        bool(item.get('history_exact')), item.get('score', 0),
        _candidate_season(item) is not None, item.get('history_mtime', 0), item.get('year', 0),
    ), reverse=True)
    for item in rows:
        item['query_errors'] = errors[:3]
    return rows[:60]


def _source_season_episode(source: dict[str, Any]) -> tuple[int | None, int | None]:
    season: int | None = None
    episode: int | None = None
    url = str(source.get('url') or '')
    match = re.search(r'(?i)\bs(\d{1,3})e(\d{1,3})\b', url)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
    if season is None:
        sm = re.search(r'\d+', str(source.get('season') or ''))
        season = int(sm.group(0)) if sm else None
    if episode is None:
        text = str(source.get('episode') or '')
        if re.search(r'(?i)пілот|pilot|спец', text):
            episode = 0
        else:
            em = re.search(r'(\d+)(?!.*\d)', text)
            episode = int(em.group(1)) if em else None
    return season, episode


def coverage_anomalies(meta: dict[str, Any], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if str(meta.get('media_type') or '') != 'tv':
        return []
    grouped: dict[int, set[int]] = {}
    specials: dict[int, set[int]] = {}
    for source in sources or []:
        if is_trailer_source(source):
            continue
        season, episode = _source_season_episode(source)
        if season is None or season <= 0 or episode is None:
            continue
        if episode <= 0:
            specials.setdefault(season,set()).add(episode)
        else:
            grouped.setdefault(season,set()).add(episode)
    tmdb_id = int(meta.get('tmdb_id') or 0)
    anomalies: list[dict[str, Any]] = []
    for season in sorted(set(grouped) | set(specials)):
        available = sorted(grouped.get(season) or set())
        expected: list[int] = []
        if tmdb_id:
            season_meta = tmdb_season_metadata(tmdb_id, season, language='en-US', force=False)
            expected = [int(v) for v in season_meta.get('episode_numbers') or [] if int(v) > 0]
            if not expected and int(season_meta.get('episode_count') or 0) > 0:
                expected = list(range(1,int(season_meta['episode_count'])+1))
        expected_set=set(expected)
        available_set=set(available)
        missing=sorted(expected_set-available_set) if expected_set else []
        ratio=(len(expected_set & available_set)/len(expected_set)) if expected_set else None
        span=(available[-1]-available[0]+1) if len(available)>=2 else len(available)
        density=(len(available)/span) if span else 1.0
        reasons=[]
        if expected_set and len(expected_set)>=4 and ratio is not None and ratio < 0.80:
            reasons.append('catalog_coverage_low')
        if len(available)>=2 and density < 0.60:
            reasons.append('nonconsecutive_sparse')
        if len(available)<=3 and available and max(available)>=8:
            reasons.append('isolated_high_episode_numbers')
        if specials.get(season) and len(available)<=2:
            reasons.append('special_plus_sparse_regular')
        if reasons:
            anomalies.append({
                'season':season,
                'available_episodes':available,
                'special_episodes':sorted(specials.get(season) or set()),
                'expected_count':len(expected_set) if expected_set else None,
                'coverage_ratio':round(ratio,4) if ratio is not None else None,
                'missing_episodes':missing[:80],
                'density':round(density,4),
                'reasons':reasons,
            })
    return anomalies


def cross_search_candidates(meta: dict[str, Any], sources: list[dict[str, Any]], *, exclude_urls: set[str] | None = None) -> dict[str, Any]:
    anomalies = coverage_anomalies(meta, sources)
    diagnostics: dict[str, Any] = {
        'version': SEARCH_VERSION,
        'triggered': bool(anomalies),
        'anomalies': anomalies,
        'queries': [], 'errors': [], 'candidates': [],
    }
    if not anomalies or not _brave_api_key():
        return diagnostics
    aliases = _search_aliases(meta)[:4]
    localized = next((a for a in aliases if re.search(r'[А-Яа-яІіЇїЄєҐґ]', a)), '')
    original = next((a for a in aliases if re.search(r'[A-Za-z]', a)), '')
    title_bits = [x for x in (localized, original, str(meta.get('title') or '')) if x]
    unique: list[str] = []
    for value in title_bits:
        if value.casefold() not in {item.casefold() for item in unique}:
            unique.append(value)
    title_clause = ' OR '.join(f'"{x}"' for x in unique[:2]) or f'"{str(meta.get("title") or "")}"'
    year = int(meta.get('year') or 0)
    existing = set(exclude_urls or set())
    found: dict[str, dict[str, Any]] = {}

    def accept(item: dict[str, Any], season: int, provider: str) -> bool:
        page_url = str(item.get('page_url') or '')
        if not page_url or page_url in existing:
            return False
        detected = _candidate_season(item)
        if detected and str(detected) != str(season):
            return False
        seed = {'title': str(meta.get('title') or meta.get('original_title') or '')}
        if float(item.get('score') or 0) < 0.50 or not _same_work_candidate(meta, seed, item):
            return False
        item['search_provider'] = provider
        item['cross_search'] = True
        item['cross_search_season'] = str(season)
        previous = found.get(page_url)
        if previous is None or float(item.get('score') or 0) > float(previous.get('score') or 0):
            found[page_url] = item
            return True
        return False

    # Anomaly-only cascade: precise season query first, then relaxed exact-title
    # provider query if the index omits season/year tokens from its snippet.
    for anomaly in anomalies[:3]:
        season = int(anomaly['season'])
        season_clause = f'("{season} сезон" OR "season {season}")'
        precise_query = f'({title_clause}) {season_clause}' + (f' {year}' if year else '')
        for domain in CROSS_SEARCH_DOMAINS:
            domain_added = 0
            diagnostics['queries'].append({'provider':'brave_cross_site','domain':domain,'season':season,'query':precise_query})
            try:
                response = _brave_get(
                    headers={'Accept':'application/json','Accept-Encoding':'gzip','X-Subscription-Token':_brave_api_key(),'User-Agent':UA},
                    params={'q':f'{precise_query} site:{domain}','country':'DE','count':10,'safesearch':'moderate'},
                    timeout=12,
                )
                response.raise_for_status()
                for node in ((response.json().get('web') or {}).get('results') or [])[:12]:
                    page_url = str(node.get('url') or '').strip()
                    host = _host(page_url)
                    if not page_url or not (host == domain or host.endswith('.' + domain)):
                        continue
                    title = ' '.join(str(node.get('title') or '').split())
                    context = title + ' ' + str(node.get('description') or '')
                    cyear = _year(context)
                    ctype = _candidate_type(page_url)
                    item = {
                        'title':title,'year':cyear,'media_type':ctype,'site':host,'page_url':page_url,
                        'score':_candidate_score(meta,title,cyear,ctype),
                    }
                    if accept(item, season, 'brave_cross_site'):
                        domain_added += 1
                if domain_added == 0:
                    for alias in unique[:2]:
                        diagnostics['queries'].append({'provider':'brave_cross_site_relaxed','domain':domain,'season':season,'query':alias})
                        for item in _brave_domain_search(alias, domain, meta):
                            item = dict(item)
                            if accept(item, season, 'brave_cross_site_relaxed'):
                                domain_added += 1
            except Exception as exc:
                diagnostics['errors'].append(f'{domain}:s{season}:{type(exc).__name__}')
    rows = sorted(found.values(), key=lambda x:(float(x.get('score') or 0), bool(_candidate_season(x))), reverse=True)[:30]
    diagnostics['candidates'] = [
        {k:x.get(k) for k in ('title','year','media_type','site','score','page_url','cross_search_season','search_provider') if x.get(k) not in (None,'')}
        for x in rows
    ]
    diagnostics['candidate_count'] = len(rows)
    return {**diagnostics, 'rows': rows}


def _qwen_rank(meta: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    try:
        cfg = json.loads(QWEN_CONFIG.read_text(encoding='utf-8'))
        host = f"{cfg['user']}@{cfg['host']}"
        key = str(Path(cfg['key_path']).expanduser())
        payload = {
            'title': meta.get('original_title') or meta.get('title'),
            'year': int(meta.get('year') or 0),
            'media_type': meta.get('media_type') or 'unknown',
            'candidates': [{k: item.get(k) for k in ('title', 'year', 'media_type', 'site', 'score')} for item in rows],
        }
        process = subprocess.run(
            ['ssh', '-i', key, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8', host, 'sudo', '/usr/local/bin/skeleton-media-rank'],
            input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True,
            timeout=105, check=False,
        )
        if process.returncode:
            return None
        result = json.loads(process.stdout)
        if result.get('schema') != 'skeleton.media.rank.v1' or result.get('status') != 'DONE':
            return None
        return result
    except Exception:
        return None


def _select(rows: list[dict[str, Any]], rank: dict[str, Any] | None) -> tuple[int, str]:
    if not rows:
        raise LookupError('Україномовні сторінки не знайдено.')
    rule_index = 0
    if rank:
        index = int(rank.get('selected_index') or 0)
        confidence = float(rank.get('confidence') or 0)
        if 0 <= index < len(rows) and confidence >= 0.72 and rows[index]['score'] >= rows[0]['score'] - 0.28:
            return index, 'qwen_guarded'
    return rule_index, 'rules'


def _persist(meta: dict[str, Any], discovery_id: str, catalog_url: str, selected: dict[str, Any] | None, rows: list[dict[str, Any]], rank: dict[str, Any] | None, status: str) -> None:
    stamp = now()
    with _db() as con:
        uid = str(meta['media_uid'])
        by_url = con.execute('SELECT media_uid FROM media_items WHERE source_url=?', (catalog_url,)).fetchone()
        if by_url and str(by_url['media_uid']) != uid:
            old_uid = str(by_url['media_uid'])
            con.execute('UPDATE discoveries SET media_uid=? WHERE media_uid=?', (uid, old_uid))
            con.execute('DELETE FROM media_items WHERE media_uid=?', (old_uid,))
        values = (
            catalog_url, str(meta.get('catalog_provider') or meta.get('provider') or 'catalog'),
            str(meta.get('media_type') or 'unknown'), str(meta.get('title') or 'Без назви'),
            str(meta.get('original_title') or ''), int(meta.get('year') or 0), meta.get('tmdb_id'),
            meta.get('imdb_id'), meta.get('poster'), str(meta.get('overview') or ''),
            json.dumps(meta, ensure_ascii=False), stamp, uid,
        )
        existing = con.execute('SELECT media_uid FROM media_items WHERE media_uid=?', (uid,)).fetchone()
        if existing:
            con.execute('UPDATE media_items SET source_url=?,provider=?,media_type=?,title=?,original_title=?,release_year=?,tmdb_id=?,imdb_id=?,poster=?,overview=?,metadata_json=?,updated_at=? WHERE media_uid=?', values)
        else:
            con.execute('INSERT INTO media_items(media_uid,source_url,provider,media_type,title,original_title,release_year,tmdb_id,imdb_id,poster,overview,metadata_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (uid, *values[:-1]))
        _persist_aliases(con, meta, catalog_url)
        details = {'metadata': meta, 'candidates': rows, 'qwen': rank}
        con.execute('''INSERT INTO discoveries(discovery_id,media_uid,catalog_url,selected_page_url,selected_score,candidate_count,qwen_used,qwen_confidence,status,details_json,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(discovery_id) DO UPDATE SET selected_page_url=excluded.selected_page_url,selected_score=excluded.selected_score,candidate_count=excluded.candidate_count,qwen_used=excluded.qwen_used,qwen_confidence=excluded.qwen_confidence,status=excluded.status,details_json=excluded.details_json,updated_at=excluded.updated_at''', (
            discovery_id, meta['media_uid'], catalog_url, selected.get('page_url') if selected else None, selected.get('score') if selected else None, len(rows), 1 if rank else 0, float(rank.get('confidence') or 0) if rank else None, status, json.dumps(details, ensure_ascii=False), stamp, stamp,
        ))


def _candidate_season(candidate: dict[str, Any]) -> str | None:
    text = ' '.join(str(candidate.get(key) or '') for key in ('title', 'page_url'))
    patterns = (
        r'\b(\d{1,3})\s*(?:сезон|season|sezon)\b',
        r'[-_/](\d{1,3})[-_](?:season|sezon)(?:[-_./]|$)',
        r'(?:^|[-_/])(?:season|sezon)[-_](\d{1,3})(?:[-_./]|$)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return str(int(match.group(1)))
    return None


def _decorate_discovered_result(result: dict[str, Any], meta: dict[str, Any], catalog_url: str, candidate: dict[str, Any]) -> dict[str, Any]:
    season = _candidate_season(candidate)
    for source in result.get('sources') or []:
        source['discovery_site'] = candidate['site']
        source['discovery_page_title'] = candidate['title']
        source['discovery_page_url'] = candidate['page_url']
        if season and not source.get('season'):
            source['season'] = season
    _decorate_source_types(result, str(meta.get('media_type') or ''))
    if season:
        result['season'] = season
    result['title'] = str(meta.get('title') or result.get('title') or candidate['title'])
    result['poster'] = meta.get('poster') or result.get('poster')
    result['page_url'] = candidate['page_url']
    result['catalog_url'] = catalog_url
    result['catalog'] = {k: meta.get(k) for k in ('media_uid','catalog_provider','media_type','title','original_title','year','tmdb_id','imdb_id','poster','overview')}
    return result


def _same_work_candidate(meta: dict[str, Any], left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not _candidate_content_compatible(meta, right):
        return False
    left_key = _history_title_key(left.get('title', ''))
    right_key = _history_title_key(right.get('title', ''))
    targets = {_history_title_key(meta.get('title', '')), _history_title_key(meta.get('original_title', '')), left_key}
    targets.discard('')
    if not right_key:
        return False
    if right_key in targets:
        return True
    if any(_proper_title_prefix(target, right_key) for target in targets):
        return False
    if any(len(target.split()) == 1 for target in targets) and _localized_single_token_match(meta, str(right.get('title') or '')):
        return True
    return max((difflib.SequenceMatcher(None, right_key, target).ratio() for target in targets), default=0.0) >= 0.88


def discover(job: dict[str, Any], resolve: Callable[[str], dict[str, Any]], *, identified_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    catalog_url = str(job.get('catalog_url') or job.get('page_url') or '')
    meta = dict(identified_meta or metadata(catalog_url))
    diagnostics = {'identified_at': now(), 'identity': {
        'media_uid': meta.get('media_uid'), 'tmdb_id': meta.get('tmdb_id'), 'imdb_id': meta.get('imdb_id'),
        'media_type': meta.get('media_type'), 'title': meta.get('title'), 'year': meta.get('year'),
    }}
    _persist(meta, str(job['job_id']), catalog_url, None, [], None, 'identified')

    if not bool(job.get('force_search')):
        negative = _recent_negative(meta)
        if negative:
            result = _base_discovery_result(meta, catalog_url, 'no_verified_sources', message='Перевірених українських джерел поки немає.', diagnostics={**diagnostics, 'negative_cache': negative})
            return result

    rows = candidates(meta, force=bool(job.get('force_search')))
    requested_season = str(meta.get('selected_season') or '')
    if requested_season:
        exact_season_rows = [row for row in rows if str(_candidate_season(row) or '') == requested_season]
        if exact_season_rows:
            rows = exact_season_rows
        else:
            # Do not silently resolve a different season for a season/episode deep-link.
            rows = []
    diagnostics['search'] = meta.get('_search_diagnostics') or {}
    diagnostics['candidate_count'] = len(rows)
    if not rows:
        _persist(meta, str(job['job_id']), catalog_url, None, [], None, 'no_verified_sources')
        return _base_discovery_result(meta, catalog_url, 'no_verified_sources', message='Перевірених українських джерел поки немає.', diagnostics=diagnostics)

    selected_index, selected_by = _select(rows, None)
    selected = rows[selected_index]
    ordered = [selected] + [item for index, item in enumerate(rows) if index != selected_index]
    if str(meta.get('media_type') or '') == 'tv':
        same_show = [item for item in ordered if _same_work_candidate(meta, selected, item)]
        if same_show:
            same_urls = {str(item.get('page_url') or '') for item in same_show}
            ordered = same_show + [item for item in ordered if str(item.get('page_url') or '') not in same_urls]

    errors: list[str] = []
    limit = 6 if str(meta.get('media_type') or '') == 'tv' else 4
    for candidate in ordered[:limit]:
        try:
            result = _resolve_candidate(candidate, meta, catalog_url, resolve)
            if not result.get('sources'):
                raise RuntimeError('no_sources')
            pending = [
                {key: item.get(key) for key in ('title','year','media_type','site','score','page_url','history_job_id') if item.get(key) not in (None,'')}
                for item in ordered if item.get('page_url') != candidate.get('page_url')
            ]
            result['status'] = 'ready'
            result['discovery'] = {
                'state': 'stream_verified', 'selected_by': selected_by,
                'selected': {key: candidate.get(key) for key in ('title','year','media_type','site','score','page_url')},
                'candidate_count': len(rows), 'pending_candidates': pending,
                'reused_without_network': bool(result.pop('discovery_reused_without_network', False)),
                'search': diagnostics.get('search') or {}, 'errors': errors[:6],
            }
            _persist(meta, str(job['job_id']), catalog_url, candidate, rows, None, 'ready')
            return result
        except Exception as exc:
            errors.append(f"{candidate.get('title')}: {type(exc).__name__}: {str(exc)[:180]}")

    diagnostics['errors'] = errors[:8]
    _persist(meta, str(job['job_id']), catalog_url, selected, rows, None, 'no_verified_sources')
    return _base_discovery_result(meta, catalog_url, 'no_verified_sources', message='Сторінки знайдено, але робочого українського стріму немає.', diagnostics=diagnostics)


def expand_one(job: dict[str, Any], resolve: Callable[[str], dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    discovery = dict(job.get('discovery') or {})
    pending = list(discovery.get('pending_candidates') or [])
    if not pending or job.get('status') not in {'ready','expanding_sources'}:
        return job, False
    candidate = pending.pop(0)
    catalog = job.get('catalog') if isinstance(job.get('catalog'), dict) else {}
    meta = dict(catalog)
    meta.setdefault('title', job.get('title'))
    meta.setdefault('poster', job.get('poster'))
    meta.setdefault('overview', job.get('overview'))
    catalog_url = str(job.get('catalog_url') or '')
    errors = list(discovery.get('errors') or [])
    try:
        part = _resolve_candidate(candidate, meta, catalog_url, resolve)
        existing = list(job.get('sources') or [])
        seen = {
            (str(source.get('season') or ''),str(source.get('episode') or ''),str(source.get('group') or source.get('translation') or ''),str(source.get('quality') or ''),str(source.get('url') or source.get('source_id') or ''))
            for source in existing
        }
        added = 0
        for source in part.get('sources') or []:
            key = (str(source.get('season') or ''),str(source.get('episode') or ''),str(source.get('group') or source.get('translation') or ''),str(source.get('quality') or ''),str(source.get('url') or source.get('source_id') or ''))
            if key in seen:
                continue
            seen.add(key); existing.append(source); added += 1
        job['sources'] = existing
        resolved = list(discovery.get('resolved_candidates') or [])
        resolved.append({**candidate, 'added_sources': added})
        discovery['resolved_candidates'] = resolved[-40:]
    except Exception as exc:
        errors.append(f"{candidate.get('title')}: {type(exc).__name__}: {str(exc)[:180]}")
    discovery['errors'] = errors[-12:]
    discovery['pending_candidates'] = pending
    discovery['state'] = 'expanding_sources' if pending else 'stream_verified'
    seasons = sorted({str(source.get('season')) for source in job.get('sources') or [] if source.get('season')}, key=lambda value: int(value) if value.isdigit() else 9999)
    discovery['available_seasons'] = seasons
    job['discovery'] = discovery
    job['status'] = 'ready'
    return job, True



def status() -> dict[str, Any]:
    with _db() as con:
        media = con.execute('SELECT COUNT(*) FROM media_items').fetchone()[0]
        discoveries = con.execute('SELECT COUNT(*) FROM discoveries').fetchone()[0]
        last = con.execute('SELECT status,updated_at,candidate_count,qwen_used FROM discoveries ORDER BY updated_at DESC LIMIT 1').fetchone()
    return {'schema':'skeleton.media.discovery.status.v1','database':str(DB),'media_items':media,'discoveries':discoveries,'last':dict(last) if last else None}


if __name__ == '__main__':
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == 'metadata':
        print(json.dumps(metadata(sys.argv[2]), ensure_ascii=False, indent=2))
    elif len(sys.argv) == 3 and sys.argv[1] == 'candidates':
        value = metadata(sys.argv[2])
        print(json.dumps({'metadata': value, 'candidates': candidates(value)}, ensure_ascii=False, indent=2))
    elif len(sys.argv) == 2 and sys.argv[1] == 'status':
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    else:
        raise SystemExit(2)
