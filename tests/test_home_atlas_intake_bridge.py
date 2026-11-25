"""Synthetic test against actual bridge AST. No network, player or production DB."""
import ast
import hashlib
import json
import sqlite3
import re
import difflib
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from review.home_atlas_intake_bridge_20261125 import BRIDGE

@pytest.fixture
def env(tmp_path):
    state=tmp_path
    jobs=state/'jobs';jobs.mkdir()
    with sqlite3.connect(state/'media-catalog.sqlite3') as c:
        c.execute('create table media_items(media_uid text,media_type text,tmdb_id integer,source_url text)')
    calls=[]
    meta={'media_uid':'synthetic-uid','title':'Example film','year':2026,'media_type':'movie','tmdb_id':123,'poster':'','overview':'Example'}
    def persist(m,jid,url,*a):
        calls.append(('persist',jid))
        with sqlite3.connect(state/'media-catalog.sqlite3') as c:
            c.execute('insert into media_items values(?,?,?,?)',(m['media_uid'],m['media_type'],m['tmdb_id'],url))
    def save(job):
        calls.append(('save',job['job_id']))
        (jobs/(job['job_id']+'.json')).write_text(json.dumps(job))
    def stable(job):
        c=job.get('catalog') or {}
        return f"tmdb:{c.get('media_type')}:{c.get('tmdb_id')}" if c.get('tmdb_id') else ''
    def key(job):return str((job.get('catalog') or {}).get('title') or job.get('title') or '').lower()
    ns=dict(STATE=state,JOBS=jobs,re=re,hashlib=hashlib,json=json,time=time,difflib=difflib,
            media_discovery=SimpleNamespace(metadata=lambda url:dict(meta),_catalog_payload=lambda m:dict(m),_persist=persist),
            _save=save,_job_path=lambda jid:jobs/(jid+'.json'),_history_stable_identity=stable,
            _history_job_key=key,_history_deletions_read=lambda:{},_browser_profile_for_device=lambda d:'',
            _language_preferences_read=lambda d:{'content_language':'uk'},pool=SimpleNamespace(submit=lambda fn,jid:calls.append(('queue',jid))),_discover_job=lambda jid:None)
    exec(compile(BRIDGE,'<atlas-bridge>','exec'),ns)
    snap={'state':{'selected_id':'tmdb-movie-123'},'catalog':[{'id':'tmdb-movie-123'}]}
    return ns,snap,calls,meta

def test_explicit_confirm_is_idempotent(env):
    ns,snap,calls,_=env
    one=ns['_atlas_confirm_catalog_intake'](snap,'phone')
    two=ns['_atlas_confirm_catalog_intake'](snap,'phone')
    assert one['status']=='saved_discovery_queued' and two['status']=='already_saved'
    assert one['job_id']==two['job_id']
    assert [x[0] for x in calls]==['persist','save','queue']

@pytest.mark.parametrize('selected',['tmdb-movie-999','https://bad.invalid','',None])
def test_unselected_or_untrusted_id_does_not_write(env,selected):
    ns,snap,calls,_=env
    snap['state']['selected_id']=selected
    with pytest.raises(ns['AtlasIntakeError']):ns['_atlas_confirm_catalog_intake'](snap,'phone')
    assert not calls

def test_unknown_controller_rejected(env):
    ns,snap,calls,_=env
    with pytest.raises(ns['AtlasIntakeError']):ns['_atlas_confirm_catalog_intake'](snap,'unknown')
    assert not calls

def test_metadata_mismatch_rejected(env):
    ns,snap,calls,meta=env
    meta['tmdb_id']=999
    with pytest.raises(ns['AtlasIntakeError']):ns['_atlas_confirm_catalog_intake'](snap,'phone')
    assert not calls

def test_tombstone_not_recreated(env):
    ns,snap,calls,_=env
    ns['_history_deletions_read']=lambda:{'example film':1}
    with pytest.raises(ns['AtlasIntakeError']):ns['_atlas_confirm_catalog_intake'](snap,'phone')
    assert not calls

def test_title_collision_no_merge(env):
    ns,snap,calls,meta=env
    (ns['JOBS']/'old.json').write_text(json.dumps({'catalog':dict(meta,tmdb_id=999),'job_id':'old'}))
    with pytest.raises(ns['AtlasIntakeError']):ns['_atlas_confirm_catalog_intake'](snap,'phone')
    assert not calls

def test_queue_failure_preserves_metadata(env):
    ns,snap,calls,_=env
    def fail(*a):raise RuntimeError('synthetic queue failure')
    ns['pool'].submit=fail
    r=ns['_atlas_confirm_catalog_intake'](snap,'phone')
    assert r['status']=='saved_queue_failed'
    job=json.loads((ns['JOBS']/(r['job_id']+'.json')).read_text())
    assert job['discovery_state']=='queue_failed' and job['sources']==[]

def test_hidden_candidate_rejected(env):
    ns,snap,calls,_=env
    snap['catalog'][0]['hidden']=True
    with pytest.raises(ns['AtlasIntakeError']):ns['_atlas_confirm_catalog_intake'](snap,'phone')
    assert not calls

def test_existing_matching_catalog_only_attaches_without_rewrite(env):
    ns,snap,calls,meta=env
    with sqlite3.connect(ns['STATE']/'media-catalog.sqlite3') as c:
        c.execute('insert into media_items values(?,?,?,?)',(meta['media_uid'],'movie',123,'https://www.themoviedb.org/movie/123'))
    result=ns['_atlas_confirm_catalog_intake'](snap,'phone')
    assert result['status']=='saved_discovery_queued'
    assert [x[0] for x in calls]==['save','queue']
