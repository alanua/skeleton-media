"""Selective adapter for the reviewed paired Atlas store and canonical Home owner.
No automatic TV launch or playback. Presentation capability stays false until a
registered launcher is verified. The existing media resolver is the only intake owner.
"""
from __future__ import annotations
import hashlib
import json
import re
from flask import abort, jsonify, request, send_from_directory
import movie_atlas as atlas

PAGE=24

class CatalogStore(atlas.AtlasStore):
    def create_bound(self, device, window, items):
        result=self.create(items)
        with self._lock:
            session=self._session(result['code'],self.clock())
            session.controller_device=device
            session.catalog_window=window
            return {**session.snapshot(self.clock()),'catalog_window':window,'controller_token':session.controller_token}

    def bound(self,code,token,device,revision=None):
        with self._lock:
            session=self._authorized(code,token,'controller',self.clock())
            if session.controller_device!=device:
                raise atlas.AtlasError('Контролер не підтверджений.',403,'forbidden')
            if revision is not None and (type(revision) is not int or revision!=session.revision):
                raise atlas.AtlasError('Стан змінився.',409,'revision_conflict')
            session.controller_seen=self.clock()
            return {**session.snapshot(self.clock()),'catalog_window':dict(session.catalog_window)}

    def replace_window(self,code,token,device,revision,window,items):
        with self._lock:
            session=self._authorized(code,token,'controller',self.clock())
            if session.controller_device!=device:
                raise atlas.AtlasError('Контролер не підтверджений.',403,'forbidden')
            if type(revision) is not int or revision!=session.revision:
                raise atlas.AtlasError('Стан змінився.',409,'revision_conflict')
            session.catalog=atlas.public_catalog(items)
            session.catalog_window=dict(window)
            session.state=atlas._default_state()
            session.revision+=1
            session.controller_seen=self.clock()
            return {**session.snapshot(self.clock()),'catalog_window':dict(window)}

    def read_window(self,code,token,role,device=None):
        if role=='controller':return self.bound(code,token,device)
        with self._lock:
            session=self._authorized(code,token,'display',self.clock())
            session.display_seen=self.clock()
            return {**session.snapshot(self.clock()),'catalog_window':dict(session.catalog_window)}

def register(app,*,require,device_id,history_rows,search_rows,confirm_intake,display_clear,static,poster_safe,presentation_ready=lambda:False):
    store=CatalogStore()
    def controller():
        require()
        device=device_id()
        if not device or device=='unknown':abort(403)
        return device
    def write():
        require()
        if request.headers.get('Origin')!=request.host_url.rstrip('/') or request.headers.get('Sec-Fetch-Site') not in (None,'same-origin'):abort(403)
        if (request.content_length or 0)>2048:abort(413)
        return controller()
    def body(keys):
        raw=request.stream.read(2049)
        if len(raw)>2048:abort(413)
        try: value=json.loads(raw) if request.is_json else None
        except (ValueError,RecursionError):value=None
        if not isinstance(value,dict) or value.keys()!=keys:abort(400)
        return value
    def token():return request.headers.get('X-Atlas-Token')
    def safe(fn):
        from functools import wraps
        @wraps(fn)
        def call(*a,**k):
            try:return fn(*a,**k)
            except atlas.AtlasError as e:return jsonify(error=str(e),code=e.code),e.status
        return call
    def clear():
        if not display_clear():raise atlas.AtlasError('Екран зайнятий або стан невідомий.',409,'display_busy')
    def tid(row):
        v=str(row.get('tmdb_id') or '')
        kind=row.get('media_type')
        return f'tmdb-{kind}-{v}' if kind in ('movie','tv') and re.fullmatch(r'[1-9][0-9]{0,9}',v) else None
    def saved_id(row):
        h=row.get('history_id')
        return f'saved-{h}' if isinstance(h,str) and re.fullmatch(r'[0-9a-f]{16}',h) else None
    def project(row,id):
        result={'id':id,'title':row.get('title'),'year':row.get('year'),'genres':row.get('genres')}
        p=row.get('poster')
        if isinstance(p,str) and atlas.POSTER_RE.fullmatch(p) and poster_safe(p):result['poster']=p
        return result
    def window(scope,query,page):
        if scope not in ('saved','tmdb') or not isinstance(query,str) or len(query)>80 or type(page) is not int or page<1:abort(400)
        rows=list(history_rows())
        if scope=='saved':
            q=query.strip().casefold()
            candidates=[project(row,saved_id(row)) for row in rows if saved_id(row) and atlas.visible(row) and q in (str(row.get('title') or '')+' '+str(row.get('original_title') or '')).casefold()]
            # Only window size is bounded. Query and count cover the full shared library.
            total=len(candidates);start=(page-1)*PAGE
            if page>1 and start>=total:abort(400)
            return dict(scope=scope,query=query,page=page,total=total,has_previous=page>1,has_next=start+PAGE<total),candidates[start:start+PAGE]
        if page!=1 or not query.strip():abort(400)
        hidden={tid(r) for r in rows if not atlas.visible(r)}
        candidates=[];seen=set()
        for row in search_rows(query.strip()):
            key=tid(row)
            if not key or key in hidden or key in seen:continue
            seen.add(key);candidates.append(project(row,key))
        # The installed proxy ignores page; no fabricated continuation.
        return dict(scope=scope,query=query,page=1,total=len(candidates),has_previous=False,has_next=False,provider_limited=True),candidates
    @app.get('/api/movie-atlas/capabilities')
    @safe
    def atlas_capabilities():
        controller()
        return jsonify(schema='skeleton.media.movie_atlas.capabilities.v1',protocol=1,supported=True,controller_path='/movie-atlas',supported_targets=['home_edge_tv'],presentation_ready=bool(presentation_ready() and display_clear()))
    @app.get('/movie-atlas')
    @app.get('/movie-atlas/display')
    def atlas_page():
        require()
        response=send_from_directory(static,'index.html')
        response.headers['Cache-Control']='no-store'
        response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response
    @app.get('/movie-atlas/exit')
    def atlas_exit():
        require()
        return '',204
    @app.post('/api/movie-atlas/sessions')
    @safe
    def atlas_create():
        d=write();clear()
        if request.stream.read(1):abort(400)
        info,items=window('saved','',1)
        return jsonify(store.create_bound(d,info,items)),201
    @app.post('/api/movie-atlas/join')
    @safe
    def atlas_join():
        write();v=body({'code'})
        return jsonify(store.join(v['code'],require_clear=clear))
    @app.get('/api/movie-atlas/sessions/<code>')
    @safe
    def atlas_read(code):
        require();role=request.args.get('role','display')
        if role=='display':
            store.read(code,token(),'display',seen=False);clear()
            return jsonify(store.read_window(code,token(),'display'))
        if role=='controller':return jsonify(store.read_window(code,token(),role,controller()))
        abort(400)
    @app.put('/api/movie-atlas/sessions/<code>')
    @safe
    def atlas_update(code):
        d=write();v=body({'revision','state'});store.bound(code,token(),d,v['revision'])
        r=store.update(code,token(),v['revision'],v['state'])
        return jsonify({**r,'catalog_window':store.bound(code,token(),d)['catalog_window']})
    @app.post('/api/movie-atlas/sessions/<code>/catalog')
    @safe
    def atlas_catalog(code):
        d=write();v=body({'revision','scope','query','page'})
        store.bound(code,token(),d,v['revision'])
        info,items=window(v['scope'],v['query'],v['page'])
        return jsonify(store.replace_window(code,token(),d,v['revision'],info,items))
    @app.post('/api/movie-atlas/sessions/<code>/handoff')
    @safe
    def atlas_handoff(code):
        d=write();v=body({'revision'})
        if type(v['revision']) is not int:abort(400)
        snap=store.bound(code,token(),d,v['revision'])
        selected=snap['state']['selected_id']
        if not any(row['id']==selected for row in snap['catalog']):raise atlas.AtlasError('Твір недоступний.',409,'selection_unavailable')
        if selected.startswith('saved-'):
            if not any(saved_id(r)==selected and atlas.visible(r) for r in history_rows()):raise atlas.AtlasError('Твір недоступний.',409,'selection_unavailable')
            return jsonify(status='already_saved',history_id=selected[6:])
        if any(tid(r)==selected and not atlas.visible(r) for r in history_rows()):raise atlas.AtlasError('Твір недоступний.',409,'selection_unavailable')
        try:result=confirm_intake(snap,d)
        except ValueError:raise atlas.AtlasError('Додавання відхилено: перевірте ідентичність твору.',409,'intake_rejected')
        return jsonify(result)
    @app.delete('/api/movie-atlas/sessions/<code>')
    @safe
    def atlas_close(code):
        d=write();store.bound(code,token(),d)
        if request.stream.read(1):abort(400)
        store.close(code,token());return jsonify(status='closed')
    return store
