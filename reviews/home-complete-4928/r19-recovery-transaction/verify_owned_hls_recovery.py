"""Bounded real local HLS starvation/reload contract; no production TV/session."""
import ast
from copy import deepcopy
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

from mpv_recovery_runtime import MPVRecoveryRuntime
from playback_intent import PlaybackIntent


def run():
    with tempfile.TemporaryDirectory(prefix='r19-owned-hls-') as directory:
        root=Path(directory);ipc=root/'mpv.sock';media=root/'media';media.mkdir()
        subprocess.run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error',
            '-f','lavfi','-i','testsrc2=size=96x64:rate=8','-t','18','-an',
            '-c:v','libx264','-preset','ultrafast','-g','8','-sc_threshold','0',
            '-f','hls','-hls_time','1','-hls_list_size','0',
            '-hls_segment_filename',str(media/'part%d.ts'),str(media/'fixture.m3u8')],
            check=True,capture_output=True,timeout=15)
        release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                name=self.path.rsplit('/',1)[-1]
                if name=='fixture.m3u8':
                    data=(media/name).read_bytes()
                elif name.startswith('part') and name.endswith('.ts'):
                    if self.path.startswith('/old/') and int(name[4:-3])>=1:
                        release.wait(15)
                    data=(media/name).read_bytes()
                else:
                    self.send_error(404);return
                try:
                    self.send_response(200);self.send_header('Content-Length',str(len(data)))
                    self.end_headers();self.wfile.write(data)
                except (BrokenPipeError,ConnectionResetError):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        http_thread=threading.Thread(target=server.serve_forever,name='owned-hls-fixture')
        http_thread.start()
        port=server.server_address[1]
        old_url='http://127.0.0.1:'+str(port)+'/old/fixture.m3u8'
        fresh_url='http://127.0.0.1:'+str(port)+'/fresh/fixture.m3u8'
        process=None
        def wire(parts,timeout=.8):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as channel:
                channel.settimeout(timeout);channel.connect(str(ipc))
                channel.sendall(json.dumps({'command':parts,'request_id':1}).encode()+b'\n')
                data=b''
                while len(data)<=131072:
                    chunk=channel.recv(65536)
                    if not chunk:raise RuntimeError('fixture IPC closed')
                    data+=chunk
                    while b'\n' in data:
                        raw,data=data.split(b'\n',1);response=json.loads(raw)
                        if response.get('request_id')==1:return response
                raise RuntimeError('fixture IPC limit')
        def prop(name):return wire(['get_property',name]).get('data')
        def atomic(path,value):
            path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            temp=path.with_name(path.name+'.tmp')
            fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as stream:
                json.dump(value,stream);stream.flush();os.fsync(stream.fileno())
            os.replace(temp,path)
        try:
            process=subprocess.Popen(['/usr/bin/mpv','--no-config','--no-terminal',
                '--load-scripts=no','--input-default-bindings=no','--vo=null','--ao=null',
                '--audio=no','--hwdec=no','--idle=yes','--keep-open=yes','--ytdl=no',
                '--cache=yes','--cache-pause=yes','--cache-pause-wait=.1',
                '--demuxer-readahead-secs=.1','--network-timeout=15',
                '--demuxer-lavf-probesize=32768','--demuxer-lavf-analyzeduration=.1',
                '--input-ipc-server='+str(ipc),'--',old_url],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError('owned mpv exited')
                try:
                    if (prop('vo-configured') is True and prop('paused-for-cache') is True
                            and prop('pause') is False and prop('time-pos') is not None):
                        break
                except (OSError,RuntimeError):pass
                time.sleep(.05)
            else:raise RuntimeError('real HLS starvation admission unavailable')
            gate=PlaybackIntent();jobs=root/'jobs';jobs.mkdir()
            mode=root/'tv-mode';mode.mkdir();(mode/'current').write_text('mpv')
            source={'source_id':'old','kind':'hls','url':old_url,'season':'','episode':'','group':''}
            fresh=dict(source,url=fresh_url,source_id='fresh')
            job={'job_id':'a'*16,'title':'Owned generated fixture','sources':[source]}
            current={'pid':process.pid,'started_at':int(time.time()),'job_id':job['job_id'],'source_id':'old'}
            atomic(jobs/(job['job_id']+'.json'),job);atomic(root/'current.json',current)
            def command(parts,timeout=.8,expected_revision=None):
                return gate.execute(parts,lambda:wire(parts,timeout),
                                    expected_revision=expected_revision)
            def refresh(job,source,*,persist,resolver):
                assert persist is False
                return deepcopy(resolver('http://127.0.0.1/owned')['sources'][0])
            player=SimpleNamespace(CURRENT=root/'current.json',JOBS=jobs,LAST_VOD=root/'last-vod.json',
                MODE_FILE=mode/'current',MODE_REQUEST_FILE=mode/'requested',
                _mode_transition_target=lambda:None,_current_tv_mode=lambda:'mpv',
                _PLAYBACK_INTENT=gate,playback_control_revision=gate.read,command=command,
                _command_wire=wire,_atomic_private=atomic,
                _mpv_source_configuration=lambda j,s,sub:(s['url'],[],{}),
                _write_last_vod=lambda j,s,position,duration,paused,reason:atomic(root/'last-vod.json',
                    dict(job_id=j['job_id'],source_id=s['source_id'],
                         position_seconds=position,paused=paused)))
            app=SimpleNamespace(_job_path=lambda jid:jobs/(jid+'.json'),
                _job_write_path=lambda j:jobs/(j['job_id']+'.json'),
                _history_source_season=lambda j,s:'',_history_source_episode=lambda j,s:'',
                _refresh_stale_source=refresh)
            worker=root/'worker.py';worker.write_text('# bounded fixture only')
            runtime=MPVRecoveryRuntime(app=app,player=player,resolver_worker=worker,root=root/'recovery')
            runtime.resolver=lambda *a,**kw:{'sources':[deepcopy(fresh)]}
            diagnostics=[]
            original_run=runtime.engine.run
            def traced_run(**kwargs):
                def wrap(name,fn):
                    def call(*args,**kw):
                        try:return fn(*args,**kw)
                        except Exception as error:
                            diagnostics.append(dict(callback=name,error_type=type(error).__name__,
                                safe_detail=str(error) if type(error).__name__ in
                                ('SnapshotUnavailable','RecoveryCommitUnavailable') else 'sanitized'))
                            raise
                    return call
                for name in ('snapshot','resolve','command','restore_tracks','commit','transport_options','reserve_budget'):
                    kwargs[name]=wrap(name,kwargs[name])
                return original_run(**kwargs)
            runtime.engine.run=traced_run
            start=prop('time-pos')
            result=runtime.run_once()
            if result.get('state')!='recovered':
                raise RuntimeError('actual HLS transaction '+str(result)+' '+str(diagnostics))
            end=prop('time-pos')
            saved=json.loads((root/'current.json').read_text())
            assert end>start+.5 and saved['source_id']=='fresh'
            assert saved['pid']==process.pid and saved['started_at']==current['started_at']
            assert prop('pause') is False and prop('paused-for-cache') is False
            assert gate.read()==0 and not list((root/'recovery/frames').rglob('*.png'))
            return dict(status='PASS',actual_local_hls_starvation=True,
                actual_mpv_load_restore_progress_commit=True,session_preserved=True,
                independent_decoded_image_changes=True,raw_images_retained=False,
                external_network_requests=0,physical_tv_output=False,
                real_production_provider_acceptance=False)
        finally:
            release.set()
            if process is not None and process.poll() is None:
                try:wire(['quit'])
                except Exception:pass
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:process.wait(timeout=2)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
            server.shutdown();server.server_close();http_thread.join(2)


if __name__=='__main__':
    try:print(json.dumps(run()))
    except Exception as error:
        print(json.dumps({'status':'FAIL','error_type':type(error).__name__,
            'error':str(error) if isinstance(error,(RuntimeError,AssertionError)) else 'private fixture failure',
            'physical_tv_output':False}))
        raise SystemExit(1)
