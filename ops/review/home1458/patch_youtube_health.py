import ast
from pathlib import Path
from patch_backend import replace_once


def patch(root,js):
    root=Path(root)
    p=root/'home-edge-chrome-media';s=p.read_text()
    node=next(n for n in ast.parse(s).body if isinstance(n,ast.Assign)
              and any(isinstance(t,ast.Name) and t.id=='STATUS_JS' for t in n.targets))
    lines=s.splitlines(keepends=True)
    s=''.join(lines[:node.lineno-1])+'STATUS_JS='+repr(Path(js).read_text().strip())+'\n'+''.join(lines[node.end_lineno:])
    ast.parse(s);p.write_text(s)

    p=root/'home-edge-youtube-stability';s=p.read_text()
    s=replace_once(s,'from pathlib import Path','from pathlib import Path\nimport runpy\nsys.path.insert(0,str(Path(__file__).resolve().parent.parent/"lib/skeleton-cast"))\nfrom youtube_playback_health import youtube_progress_health,canonical_media_mode')
    helper='''def _mode_name(mode:str)->str:
    return canonical_media_mode(mode)

def youtube_health()->dict:
    probe=runpy.run_path(str(Path.home()/'.local/bin/home-edge-chrome-media'))['status']
    before=probe();started=time.monotonic();time.sleep(3.2);after=probe()
    return youtube_progress_health(before,after,time.monotonic()-started)

'''
    s=replace_once(s,'def status()->dict:',helper+'def status()->dict:')
    s=replace_once(s,"    return {'status':'ok','mode':mode,'player':player}","    health=youtube_health() if _mode_name(mode)=='kiosk' else {'state':'not_applicable','healthy':False}\n    return {'status':'ok','mode':mode,'player':player,'youtube_health':health}")
    a=s.index('def recover()->dict:');b=s.index('    cache=json.loads(CACHE.read_text',a)
    prefix='''def recover()->dict:
    mode=subprocess.run([str(Path.home()/'.local/bin/tv-mode'),'status'],text=True,capture_output=True,timeout=10,check=False).stdout.strip()
    if _mode_name(mode)!='kiosk':
        return {'status':'not_recovered','reason':'different_active_mode'}
    health=youtube_health()
    before=http('/api/player',timeout=8)
    if health['state']=='paused' or before.get('pause'):
        return {'status':'not_recovered','reason':'paused_by_user','youtube_health':health}
    if health['healthy']:
        return {'status':'already_stable','reason':'clock_and_decoded_frames_advance','youtube_health':health}
    if health['state'] not in {'stalled','frame_stalled'}:
        return {'status':'not_recovered','reason':'progress_unverified','youtube_health':health}
'''
    s=s[:a]+prefix+s[b:]
    ast.parse(s);p.write_text(s)


if __name__=='__main__':
    import sys
    patch(sys.argv[1],sys.argv[2])
