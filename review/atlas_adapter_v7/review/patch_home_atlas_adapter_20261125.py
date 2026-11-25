"""Bind only reviewed Atlas routes to the exact private paired backend."""
import hashlib
BASE="f3d4a07b7451fb1c7d79d04cb6b7d11308c406fd7cd0c514ba4a81d4e34b0b81"
BINDING='''
# Selective Atlas adapter; no autonomous display launch or new persistence owner.
def _atlas_live_rows():
    rows = [_history_item(group) for group in _video_history_groups().values()]
    rows.sort(key=lambda row: max(int(row.get('added_at') or 0),int(row.get('last_viewed_at') or 0)), reverse=True)
    return rows

def _atlas_live_search(query):
    data = media_discovery._proxy('/v1/search',{'query':query,'language':media_discovery.preferred_content_locale()})
    if not isinstance(data,dict) or not isinstance(data.get('results'),list):
        raise movie_atlas_live_adapter.atlas.AtlasError('TMDB недоступний.',503,'provider_unavailable')
    return [r for r in data['results'] if isinstance(r,dict)]

def _atlas_live_display_clear():
    try:
        result = subprocess.run([str(HOME / '.local/bin/home-edge-media-display-owner'),'status'],capture_output=True,text=True,timeout=6,check=False)
        report = json.loads(result.stdout)
        if result.returncode != 1 or report.get('schema_version') != 'skeleton.home_edge.media_display_ownership.v1' or report.get('state') != 'CLEAR':
            return False
        return report.get('reason') == 'display_mode_off' or (report.get('reason') == 'confirmed_video_not_playing' and any(o.get('source')=='skeleton_cast_player' and o.get('state')=='CLEAR' and o.get('reason')=='player_running_false' for o in report.get('observations',[])))
    except (OSError,ValueError,TypeError,subprocess.SubprocessError):
        return False

def _atlas_live_poster_safe(poster):
    from PIL import Image
    path=POSTERS / poster.split('?',1)[0].rsplit('/',1)[-1]
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size>512*1024:return False
        with Image.open(path,formats=['JPEG','PNG','WEBP','AVIF']) as img:
            return img.width<=960 and img.height<=960 and getattr(img,'n_frames',1)==1
    except (OSError,ValueError,Image.DecompressionBombError):return False

import movie_atlas_live_adapter
_ATLAS_LIVE = movie_atlas_live_adapter.register(app, require=_require, device_id=_request_device_id,
    history_rows=_atlas_live_rows, search_rows=_atlas_live_search, confirm_intake=_atlas_confirm_catalog_intake,
    display_clear=_atlas_live_display_clear, static=STATIC/'movie_atlas', poster_safe=_atlas_live_poster_safe)
# Native capability remains presentation_ready=false until actual registered
# TV presentation launch and physical acceptance are proven separately.
'''
def patch(s):
    if hashlib.sha256(s.encode()).hexdigest()!=BASE:raise ValueError("ATLAS_ADAPTER_BASE_MISMATCH")
    anchor="\nif __name__ == '__main__':"
    if s.count(anchor)!=1:raise ValueError("ATLAS_ADAPTER_ANCHOR_MISMATCH")
    result=s.replace(anchor,BINDING+anchor)
    compile(result,'<atlas-adapter-review>','exec')
    return result
