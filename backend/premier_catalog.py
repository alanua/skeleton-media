"""Bounded reader for the reviewed PREMIER anchor. Listing is not playability."""
import datetime as dt
from html.parser import HTMLParser
import http.client
import json
import io
import re
import time

# Same reviewed PR4822 anchor; existing catalog UIDs are never derived/remapped here.
URL = 'https://premier.one/show/reportazh-2035'
MAX_HTML = 2_000_000
MAX_JSON = 1_000_000


def validate_url(url):
    if url != URL:
        raise ValueError('PREMIER_CANONICAL_URL')


class Capture(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.canonicals = []
        self.payloads = []
        self.active = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'link' and 'canonical' in a.get('rel', '').lower().split():
            self.canonicals.append(a.get('href'))
        if tag == 'script' and a.get('id') == '__NUXT_DATA__':
            if a.get('type') != 'application/json' or self.active:
                raise ValueError('PREMIER_SCHEMA')
            self.payloads.append('')
            self.active = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.payloads[-1] += data


def parse_catalog(html, url=URL, *, observed_at=None):
    validate_url(url)
    if not isinstance(html, bytes) or len(html) > MAX_HTML:
        raise ValueError('PREMIER_HTML_LIMIT')
    p = Capture()
    p.feed(html.decode('utf-8', errors='strict'))
    if p.canonicals != [URL] or len(p.payloads) != 1 or p.active:
        raise ValueError('PREMIER_CANONICAL_OR_SCHEMA')
    if len(p.payloads[0].encode()) > MAX_JSON:
        raise ValueError('PREMIER_JSON_LIMIT')
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError('PREMIER_DUPLICATE_JSON_KEY')
            out[k] = v
        return out
    try:
        slots = json.loads(p.payloads[0], object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('PREMIER_JSON')))
    except (RecursionError, json.JSONDecodeError) as exc:
        raise ValueError('PREMIER_JSON') from exc
    if not isinstance(slots, list) or not 1 <= len(slots) <= 20000:
        raise ValueError('PREMIER_SCHEMA')
    # Bound raw JSON structure independently of reference traversal.
    stack = [(slots, 0)]; count = 0
    while stack:
        value, depth = stack.pop(); count += 1
        if count > 100000 or depth > 64:
            raise ValueError('PREMIER_NODE_LIMIT')
        if isinstance(value, (dict, list)):
            stack.extend((v, depth + 1) for v in (value.values() if isinstance(value, dict) else value))
    budget = 0
    def ref(index, path=()):
        nonlocal budget
        budget += 1
        if budget > 10000 or len(path) > 32:
            raise ValueError('PREMIER_REFERENCE_LIMIT')
        if type(index) is not int or not 0 <= index < len(slots) or index in path:
            raise ValueError('PREMIER_REFERENCE')
        value = slots[index]
        if isinstance(value, dict):
            return {k: ref(v, path + (index,)) for k, v in value.items()}
        if isinstance(value, list):
            return [ref(v, path + (index,)) for v in value]
        return value
    matches = []
    for obj in slots:
        if isinstance(obj, dict) and {'id', 'slug', 'type', 'seasons', 'videoEpisodes'} <= obj.keys():
            if ref(obj['id']) == 247905 or ref(obj['slug']) == 'reportazh-2035':
                matches.append(obj)
    if len(matches) != 1:
        raise ValueError('PREMIER_SERIES_BINDING')
    series = matches[0]
    kind = ref(series['type'])
    if ref(series['id']) != 247905 or ref(series['slug']) != 'reportazh-2035' or not isinstance(kind, dict) or kind.get('id') != 2 or kind.get('nameEn') != 'Series':
        raise ValueError('PREMIER_SERIES_BINDING')
    if not isinstance(ref(series['seasons']), list):
        raise ValueError('PREMIER_SEASONS')
    bound = slots[series['videoEpisodes']] if type(series['videoEpisodes']) is int and 0 <= series['videoEpisodes'] < len(slots) else None
    if not isinstance(bound, dict) or set(bound) != {'pagination', 'result'}:
        raise ValueError('PREMIER_EPISODES_SCHEMA')
    pagination = ref(bound['pagination'])
    refs = slots[bound['result']] if type(bound['result']) is int and 0 <= bound['result'] < len(slots) else None
    if not isinstance(refs, list) or len(refs) > 100 or any(type(pagination.get(k)) is not int for k in ('page','limit','pages','items')):
        raise ValueError('PREMIER_PAGINATION')
    if pagination['page'] != 1 or pagination['pages'] != 1 or not 1 <= pagination['limit'] <= 100 or pagination['items'] != len(refs) or len(refs) > pagination['limit']:
        raise ValueError('PREMIER_PAGINATION')
    observed_at = time.time() if observed_at is None else observed_at
    now = dt.datetime.fromtimestamp(observed_at, dt.timezone.utc)
    def date_flag(value, start):
        if value is None:
            return True
        if not isinstance(value, str):
            raise ValueError('PREMIER_DATE')
        try:
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            # Provider timezone is unspecified for naive non-null dates: fail closed.
            if parsed.tzinfo is None:
                raise ValueError('timezone required')
        except ValueError as exc:
            raise ValueError('PREMIER_DATE') from exc
        return parsed <= now if start else parsed > now
    episodes = []; seen = set(); ids = set()
    for index in refs:
        if type(index) is not int or not 0 <= index < len(slots) or not isinstance(slots[index], dict):
            raise ValueError('PREMIER_REFERENCE')
        obj = slots[index]
        fields = ('type','id','season','episode','duration','isClickable','startDate','endDate','availableTo')
        if not set(fields) <= obj.keys():
            raise ValueError('PREMIER_EPISODE_SCHEMA')
        e = {k: ref(obj[k], (index,)) for k in fields}
        if not isinstance(e['type'], dict) or e['type'].get('id') != 6 or not isinstance(e['id'], str) or not re.fullmatch('[0-9a-fA-F]+', e['id']):
            raise ValueError('PREMIER_EPISODE_TYPE')
        if any(type(e[k]) is not int or not 0 < e[k] <= 100000 for k in ('season','episode','duration')) or type(e['isClickable']) is not bool:
            raise ValueError('PREMIER_EPISODE_FLAGS')
        key = (e['season'], e['episode'])
        if key in seen or e['id'] in ids:
            raise ValueError('PREMIER_DUPLICATE_EPISODE')
        seen.add(key); ids.add(e['id'])
        flags = [date_flag(e['startDate'], True), date_flag(e['endDate'], False), date_flag(e['availableTo'], False)]
        eligible = e['isClickable'] and all(flags)
        episodes.append({'release_key':f"S{key[0]:02d}E{key[1]:02d}", 'season':key[0], 'episode':key[1],
                         'air_date':None, 'release_date':None, 'episode_title':'', 'catalog_state':'CATALOG_LISTED' if eligible else 'ANNOUNCED', 'eligible':eligible})
    return {'episodes':episodes, 'observed_at':observed_at, 'canonical_url':URL, 'series_id':247905}


def _fetch(url, timeout, cap):
    validate_url(url)
    deadline = time.monotonic() + timeout
    con = http.client.HTTPSConnection('premier.one', timeout=timeout)
    class DeadlineReader(io.RawIOBase):
        def __init__(self, sock):
            self.sock = sock
            self.total = 0
        def readable(self):
            return True
        def readinto(self, buf):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('PREMIER_DEADLINE')
            self.sock.settimeout(remaining)
            size = self.sock.recv_into(buf)
            self.total += size
            if self.total > cap + 65536:
                raise ValueError('PREMIER_WIRE_LIMIT')
            return size
    class DeadlineSocket:
        def __init__(self, sock):
            self.sock = sock
        def makefile(self, mode):
            return io.BufferedReader(DeadlineReader(self.sock))
    con.response_class = lambda sock, **kw: http.client.HTTPResponse(DeadlineSocket(sock), **kw)
    try:
        con.request('GET', '/show/reportazh-2035', headers={'Accept':'text/html', 'Accept-Encoding':'identity', 'User-Agent':'Skeleton-Home-Metadata/1'})
        con.sock.settimeout(max(.001, deadline-time.monotonic()))
        response = con.getresponse()
        if response.status != 200:  # No redirect is followed, including off-host redirects.
            raise ValueError(f'PREMIER_HTTP_{response.status}')
        expected = response.length
        chunks = []; size = 0
        while True:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('PREMIER_DEADLINE')
            if con.sock is not None:
                con.sock.settimeout(remaining)
            chunk = response.read1(min(65536, cap + 1 - size))
            if not chunk:
                break
            chunks.append(chunk); size += len(chunk)
            if size > cap:
                raise ValueError('PREMIER_HTML_LIMIT')
        if expected is not None and size != expected:
            raise http.client.IncompleteRead(b''.join(chunks), expected - size)
        return url, b''.join(chunks)
    finally:
        con.close()


def current_catalog(url=URL, *, fetch=None):
    validate_url(url)
    started = time.monotonic()
    final, body = (fetch or _fetch)(url, 8, MAX_HTML)
    if time.monotonic()-started > 8:
        raise TimeoutError('PREMIER_DEADLINE')
    validate_url(final)
    return parse_catalog(body, final)
