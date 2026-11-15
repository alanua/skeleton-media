"""Patch an isolated copy of the verified private baseline; no live writes."""
import ast
from pathlib import Path


def replace_once(s,old,new):
    assert s.count(old)==1, ('source_boundary',old[:90],s.count(old))
    return s.replace(old,new)


def wrap_route(s,name,*,context=False,lock=True):
    n=next(n for n in ast.parse(s).body if isinstance(n,ast.FunctionDef) and n.name==name)
    lines=s.splitlines(keepends=True)
    body=''.join(lines[n.body[0].lineno-1:n.end_lineno])
    assert body.startswith('    _require()\n')
    rest=body[len('    _require()\n'):]
    prefix=''
    if context:
        prefix="    context_data=request.get_json(silent=True) or {}\n    _require_media_control_context(endpoint_id,context_data)\n"
    new='    _require()\n'
    if lock:new+='    with _MODE_LOCK:\n'+''.join('    '+line if line.strip() else line for line in (prefix+rest).splitlines(keepends=True))
    else:new+=prefix+rest
    return ''.join(lines[:n.body[0].lineno-1])+new+''.join(lines[n.end_lineno:])


def patch(root):
    root=Path(root)
    p=root/'app.py';s=p.read_text()
    s=replace_once(s,'import media_release_monitor','from source_page_provenance import verified_group_source_sites\nfrom media_control_context import require_expected_mode, StaleMediaControl\nimport media_release_monitor')
    s=replace_once(s,"        'history_id': group_id,","        **verified_group_source_sites(group),\n        'history_id': group_id,")
    helper='''def _require_media_control_context(endpoint_id: str, data: dict) -> None:
    if 'expected_mode' not in data:
        return
    alias=_media_endpoint_alias_required(endpoint_id)
    state=_media_endpoint_mode(alias)
    try:
        require_expected_mode(data,state.get('mode'))
    except StaleMediaControl:
        # No player/key/seek command has been sent at this point.
        from flask import abort
        abort(409,description='Режим змінився. Оновіть пульт і повторіть команду.')


'''
    s=replace_once(s,'def media_endpoint_remote_control_v1(endpoint_id: str) -> Response:',helper+'def media_endpoint_remote_control_v1(endpoint_id: str) -> Response:')
    # Move the new helper above the route decorator, preserving registration.
    deco="@app.post('/api/media/endpoints/<endpoint_id>/remote/control')\n"
    s=replace_once(s,deco+helper,helper+deco)
    for name in ['media_endpoint_remote_control_v1','media_endpoint_control_v1','media_endpoint_seek_v1']:
        s=wrap_route(s,name,context=True)
    # Same lock as mode_set: admission and command cannot interleave with endpoint mode switching.
    s=wrap_route(s,'media_endpoint_mode_post_v1')
    ast.parse(s);p.write_text(s)

    p=root/'player.py';s=p.read_text()
    # Insert after the future import, preserving Python's import ordering rule.
    marker='from __future__ import annotations'
    s=replace_once(s,marker,marker+'\nfrom source_page_provenance import source_page_provenance')
    s=replace_once(s,"        **current,\n        'display-title': display_title,","        **{k:v for k,v in current.items() if k not in {'source_page_url','source_sites'}},\n        **source_page_provenance({'jobs':[(0,job)]},job_id=job_id,source_id=source_id),\n        'display-title': display_title,")
    s=replace_once(s,"    if raw == 'mpv':\n        return 'mpv', None","    if raw in {'mpv','tv'}:\n        return raw, None")
    s=replace_once(s,"    if backend == 'kiosk':\n        web = _youtube_web_media_status(force=True)","    if backend in {'games','off'}:\n        return {'running':False,'backend':backend}\n    if backend == 'kiosk':\n        web = _youtube_web_media_status(force=True)")
    s=replace_once(s,"        'source_id': metadata.get('source_id'),","        'source_id': metadata.get('source_id'),\n        'source_page_url': metadata.get('source_page_url'),\n        'source_sites': metadata.get('source_sites'),")
    ast.parse(s);p.write_text(s)

    p=root/'home-edge-capture-boot-vod-target';s=p.read_text()
    s=replace_once(s,'import json, os, pathlib, time','import json, os, pathlib, time\nimport sys\nsys.path.insert(0,str(pathlib.Path(__file__).resolve().parent.parent/"lib/skeleton-cast"))\nfrom boot_vod_capture import load_last_vod')
    s=replace_once(s,"if current.get('boot_id')==BOOT_ID", "if isinstance(current,dict) and current.get('boot_id')==BOOT_ID")
    s=replace_once(s,"last=json.loads(LAST.read_text(encoding='utf-8'))", "last=load_last_vod(LAST)\nif last is None:\n    print(json.dumps({'schema':'skeleton.media.boot_target_capture.v1','status':'skipped','reason':'invalid_last_vod','boot_id':BOOT_ID},ensure_ascii=False));raise SystemExit(0)")
    ast.parse(s);p.write_text(s)


if __name__=='__main__':
    import sys
    patch(sys.argv[1])
