"""Review-only extension of exact private history projection. No network or write in read APIs."""
import hashlib
BASE="3c7a4d11361a054cb498a9e9319677893d7584d9683cd7b3374271951b0b4207"
def once(s,a,b):
    if s.count(a)!=1:raise ValueError("PROJECTION_ANCHOR_MISMATCH "+a[:80])
    return s.replace(a,b,1)
def patch(s):
    if hashlib.sha256(s.encode()).hexdigest()!=BASE:raise ValueError("PROJECTION_BASE_MISMATCH")
    a=s.index('def _history_item(group: dict)')
    b=s.index('\ndef ',a+5)
    part=s[a:b]
    part=once(part,'    full_sources = 0','    full_sources = 0\n    audio_languages: set[str] = set()')
    part=once(part,'            else: full_sources += 1','''            else:
                full_sources += 1
                language = str(source.get('audio_language') or '').strip().lower()
                if language in {'uk','ru','en'} and source.get('has_audio') is not False:
                    audio_languages.add(language)''')
    part=once(part,"        'title': title,","""        'title': title,
        'original_title': str(catalog.get('original_title') or ''),
        'genres': [g for g in runtime_meta.get('genres', []) if isinstance(g, str) and g.strip()] if isinstance(runtime_meta.get('genres'), list) else [],
        'audio_languages': sorted(audio_languages),""")
    s=s[:a]+part+s[b:]
    anchor='def _history_page_payload(base: dict) -> dict:'
    fn='''def _library_update_projection(items):
    # One read-only query per refreshed catalog; no monitor registration or network.
    import sqlite3
    statuses = {}
    try:
        with sqlite3.connect((STATE / 'media-catalog.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=1) as con:
            for tid, kind, enabled, state, pending in con.execute(
                "SELECT tmdb_id,media_type,enabled,state,pending_release_key FROM media_release_monitors WHERE tmdb_id>0"
            ):
                key=(int(tid),str(kind))
                status='pending_release' if enabled and pending else ('monitored' if enabled else 'off')
                if key in statuses and statuses[key]!=status: statuses[key]='unknown'
                else: statuses[key]=status
    except (sqlite3.Error, OSError, ValueError):
        statuses={}
    for item in items:
        key=(int(item.get('tmdb_id') or 0),str(item.get('media_type') or ''))
        item['library_update_state'] = 'awaiting_full_source' if item.get('source_state') in {'no_source','trailer_only'} else statuses.get(key,'unknown')


'''
    s=once(s,anchor,fn+anchor)
    a=s.index('def video_history_get()')
    b=s.index('\ndef ',a+5)
    part=s[a:b]
    part=once(part,"    payload = {'items': items,", "    _library_update_projection(items)\n    payload = {'items': items,")
    s=s[:a]+part+s[b:]
    s=once(s,"paths = (JOBS, VIDEO_HISTORY_DELETIONS, VIDEO_HISTORY_PREFERENCES)","paths = (JOBS, VIDEO_HISTORY_DELETIONS, VIDEO_HISTORY_PREFERENCES, STATE / 'media-catalog.sqlite3', STATE / 'media-catalog.sqlite3-wal', STATE / 'tmdb-work-details.json')")
    compile(s,'<review-source>','exec')
    return s
