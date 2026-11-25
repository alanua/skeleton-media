"""Exact-source Atlas intake bridge, no exposed route until paired adapter review."""
import hashlib
BASE="6d20e7e4e98b51b658553a582ab2716392258681f21c3fea711189cc483dcfd9"

BRIDGE='''class AtlasIntakeError(ValueError):
    pass


def _atlas_confirm_catalog_intake(snapshot: dict, sender_device_id: str) -> dict:
    """Internal bridge. Caller MUST authenticate controller + explicit confirmation.
    Snapshot must come from the server-owned paired session, never request JSON.
    No selection, playback, display, or endpoint progress mutation.
    """
    import fcntl
    import sqlite3
    selected = (snapshot.get('state') or {}).get('selected_id')
    candidates = [row for row in snapshot.get('catalog', []) if isinstance(row, dict) and row.get('id') == selected]
    match = re.fullmatch(r'tmdb-(movie|tv)-([1-9][0-9]{0,9})', selected or '') if isinstance(selected, str) else None
    if len(candidates) != 1 or not match or candidates[0].get('hidden') or candidates[0].get('deleted'):
        raise AtlasIntakeError('ATLAS_SELECTION_NOT_AVAILABLE')
    if not sender_device_id or sender_device_id == 'unknown':
        raise AtlasIntakeError('ATLAS_CONTROLLER_NOT_VERIFIED')
    kind, raw_id = match.groups()
    tid = int(raw_id)
    identity = f'tmdb:{kind}:{tid}'
    jid = hashlib.sha256(('atlas-intake:' + identity).encode()).hexdigest()[:16]
    url = f'https://www.themoviedb.org/{kind}/{tid}'
    # Only serialize explicit Atlas confirms. The legacy share path is independent
    # and requires a subsequent common intake-owner migration.
    lock_path = STATE / '.atlas-intake.lock'
    with lock_path.open('a', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        deleted = _history_deletions_read()
        existing = []
        for path in JOBS.glob('*.json'):
            try:
                old = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(old, dict) or old.get('superseded_by_job_id') or old.get('request_kind') == 'history_reuse':
                continue
            if _history_stable_identity(old) == identity:
                if deleted.get(_history_job_key(old)):
                    raise AtlasIntakeError('ATLAS_TOMBSTONED')
                existing.append(old)
        if existing:
            old = sorted(existing, key=lambda j: (j.get('status') != 'ready', str(j.get('job_id') or '')))[0]
            return {'status':'already_saved','job_id':old.get('job_id'),'tmdb_id':tid,'media_type':kind,'intake_state':old.get('status')}
        meta = media_discovery.metadata(url)
        if (str(meta.get('media_type')) != kind or str(meta.get('tmdb_id')) != raw_id
                or not meta.get('title') or not meta.get('media_uid') or not meta.get('year')):
            raise AtlasIntakeError('ATLAS_IDENTITY_MISMATCH')
        catalog = media_discovery._catalog_payload(meta)
        stamp = int(time.time())
        job = {'job_id':jid,'status':'no_verified_sources','discovery_state':'awaiting_full_source',
               'request_kind':'catalog_discovery','atlas_intake':True,'catalog_url':url,'page_url':url,
               'site_host':'www.themoviedb.org','sender_device_id':sender_device_id,
               'browser_profile':_browser_profile_for_device(sender_device_id),
               'content_language':_language_preferences_read(sender_device_id).get('content_language','uk'),
               'title':meta['title'],'poster':meta.get('poster'),'overview':meta.get('overview') or '',
               'catalog':catalog,'history_media_type':kind,'sources':[],
               'created_at':stamp,'user_requested_at':stamp,'started_at':stamp,
               'atlas_source_watch_requested':True,'atlas_source_watch_interval_days':7}
        key = _history_job_key(job)
        if not key or deleted.get(key):
            raise AtlasIntakeError('ATLAS_TOMBSTONED_OR_EMPTY_KEY')
        # Legacy canonical grouping still has title/similarity collisions.
        # Refuse a potentially destructive merge rather than rekey existing works.
        for path in JOBS.glob('*.json'):
            try:
                old = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if not isinstance(old, dict) or old.get('superseded_by_job_id'):
                continue
            old_key = _history_job_key(old)
            old_catalog = old.get('catalog') or {}
            if old_key == key or (str(old_catalog.get('year')) == str(meta['year']) and
                                  difflib.SequenceMatcher(None, key, old_key).ratio() >= 0.94):
                raise AtlasIntakeError('ATLAS_HISTORY_IDENTITY_COLLISION')
        if _job_path(jid).exists():
            raise AtlasIntakeError('ATLAS_JOB_ID_COLLISION')
        # Do not overwrite an existing catalog alias/identity. Existing matching
        # metadata without a job can be attached using its canonical uid.
        with sqlite3.connect((STATE / 'media-catalog.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=2) as con:
            rows = con.execute("SELECT media_uid,media_type,tmdb_id FROM media_items WHERE (tmdb_id=? AND media_type=?) OR media_uid=? OR source_url=?",
                               (tid,kind,meta['media_uid'],url)).fetchall()
        if rows and (len(rows)!=1 or tuple(rows[0])!=(meta['media_uid'],kind,tid)):
            raise AtlasIntakeError('ATLAS_CATALOG_IDENTITY_COLLISION')
        if not rows:
            media_discovery._persist(meta,jid,url,None,[],None,'no_verified_sources')
        _save(job)
        # Existing discovery owner; never /api/share (which changes selection).
        # No claim of scheduled weekly checking: current monitor is explicit opt-in.
        try:
            pool.submit(_discover_job,jid)
        except Exception:
            job['discovery_state']='queue_failed'
            _save(job)
            return {'status':'saved_queue_failed','job_id':jid,'tmdb_id':tid,'media_type':kind}
        return {'status':'saved_discovery_queued','job_id':jid,'tmdb_id':tid,'media_type':kind}


'''

def once(s,a,b):
    if s.count(a)!=1: raise ValueError("ATLAS_ANCHOR_MISMATCH: "+a[:100])
    return s.replace(a,b,1)

def patch(s):
    if hashlib.sha256(s.encode()).hexdigest()!=BASE: raise ValueError("ATLAS_BASE_MISMATCH")
    anchor="@app.post('/api/share')\ndef share() -> Response:"
    s=once(s,anchor,BRIDGE+anchor)
    # Retain an identified Atlas card during a resolver failure or in-flight lookup.
    s=once(s,"        if status not in {'ready','no_verified_sources'}:\n            continue",
           "        if status not in {'ready','no_verified_sources'} and not (job.get('atlas_intake') and status in {'discovering','searching_sources','provider_error'} and _history_stable_identity(job)):\n            continue")
    # Derived visibility only: manual hidden remains authoritative.
    s=once(s,"        'hidden': bool(stored_preference.get('hidden', False)) if isinstance(stored_preference, dict) else False,",
           "        'hidden': (bool(stored_preference.get('hidden', False)) if isinstance(stored_preference, dict) else False) or (any(j.get('atlas_intake') for _, j in jobs) and not full_sources),")
    compile(s,'<atlas-review>','exec')
    return s
