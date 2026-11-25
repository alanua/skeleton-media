"""Review-only explicit session lifecycle wiring for the existing Atlas owner."""
def once(s,a,b):
    if s.count(a)!=1:raise ValueError('anchor '+a[:100])
    return s.replace(a,b,1)

def patch_adapter(s):
    s=once(s,"atlas_owned=lambda code:False):","atlas_owned=lambda code:False,launch_display=None,close_display=None,target_ready=lambda:True):")
    s=once(s,"    store=CatalogStore()","    import threading\n    lifecycle=threading.Lock()\n    store=CatalogStore()")
    s=once(s,"presentation_ready=bool(presentation_ready() and display_clear())","presentation_ready=bool(launch_display and close_display and presentation_ready() and target_ready() and display_clear())")
    s=once(s,"        d=write();clear()\n        if request.stream.read(1):abort(400)\n        info,items=window('saved','',1)\n        return jsonify(store.create_bound(d,info,items)),201",'''        d=write()
        if request.stream.read(1):abort(400)
        with lifecycle:
            clear()
            if launch_display is not None and (not close_display or not presentation_ready() or not target_ready()):
                raise atlas.AtlasError('Запуск екрана недоступний.',409,'presentation_unavailable')
            info,items=window('saved','',1)
            snap=store.create_bound(d,info,items)
            if launch_display is not None:
                code=snap['code']
                try:
                    launched=launch_display(code)
                    if not launched or not atlas_owned(code):
                        raise RuntimeError('launch not verified')
                except Exception:
                    # Release only the exact attempted lease, never another screen.
                    try:close_display(code)
                    except Exception:pass
                    store.close(code,snap['controller_token'])
                    raise atlas.AtlasError('Не вдалося запустити екран.',409,'presentation_failed')
            return jsonify(snap),201''')
    s=once(s,"        d=write();store.bound(code,token(),d)\n        if request.stream.read(1):abort(400)\n        store.close(code,token());return jsonify(status='closed')",'''        d=write()
        if request.stream.read(1):abort(400)
        with lifecycle:
            store.bound(code,token(),d)
            if close_display is not None:
                try:
                    if not close_display(code):raise RuntimeError('close not verified')
                except Exception:
                    raise atlas.AtlasError('Не вдалося завершити екран.',409,'presentation_close_failed')
            store.close(code,token())
            return jsonify(status='closed')''')
    compile(s,'<atlas-lifecycle>','exec')
    return s

def patch_state(s):
    return once(s,"create: () => call('/sessions', 'POST'),","create: () => call('/sessions', 'POST', null, undefined, 25000),")

def patch_js(s):
    s=once(s,"        pair(() => api.join(launchCode));",'''        // Browser may render before the serialized owner commits its mode.
        // Only retry the explicit local pair code for a bounded window.
        (async () => {
          for (let attempt = 0; attempt < 8 && !sync; attempt++) {
            await pair(() => api.join(launchCode));
            if (!sync) await new Promise(resolve => setTimeout(resolve, 750));
          }
        })();''')
    return s
