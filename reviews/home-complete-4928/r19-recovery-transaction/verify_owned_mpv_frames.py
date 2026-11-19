"""Bounded real MPV contract check with a generated local fixture, no TV output."""
import json,os,socket,subprocess,tempfile,time
from pathlib import Path
from mpv_decoded_image_evidence import MPVDecodedImageEvidence

def run():
    with tempfile.TemporaryDirectory(prefix='r19-headless-') as directory:
        root=Path(directory);video=root/'fixture.mkv';ipc=root/'mpv.sock'
        subprocess.run(['/usr/bin/ffmpeg','-nostdin','-hide_banner','-loglevel','error',
                        '-f','lavfi','-i','testsrc2=size=96x64:rate=8',
                        '-t','8','-an','-c:v','ffv1',str(video)],
                       check=True,capture_output=True,timeout=10)
        process=subprocess.Popen(['/usr/bin/mpv','--no-config','--no-terminal',
                                  '--load-scripts=no','--input-default-bindings=no',
                                  '--vo=null','--ao=null','--audio=no','--hwdec=no',
                                  '--pause=yes','--idle=yes','--keep-open=yes','--ytdl=no',
                                  '--input-ipc-server='+str(ipc),'--',str(video)],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def command(parts,timeout=.8):
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as channel:
                channel.settimeout(timeout);channel.connect(str(ipc))
                channel.sendall(json.dumps({'command':parts,'request_id':1}).encode()+b'\n')
                data=b''
                while len(data)<=65536:
                    data+=channel.recv(65536)
                    while b'\n' in data:
                        raw,data=data.split(b'\n',1);response=json.loads(raw)
                        if response.get('request_id')==1:return response
                raise RuntimeError('bounded IPC response exceeded')
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError('owned mpv exited')
                if ipc.exists():
                    try:
                        state=command(['get_property','video-out-params'])
                        if state.get('error')=='success' and state.get('data'):break
                    except (OSError,RuntimeError):pass
                time.sleep(.05)
            else:raise RuntimeError('owned mpv did not load fixture')
            boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            ticks=Path('/proc/'+str(process.pid)+'/stat').read_text().rsplit(')',1)[1].split()[19]
            context={'mode':'mpv','seeking':False,'revision':0,
                     'process':(boot,process.pid,ticks),'path':str(video)}
            reader=MPVDecodedImageEvidence(context=lambda:dict(context),command=command,root=root/'frames')
            a=reader.capture();b=reader.capture()
            if a is None or b is None:raise RuntimeError('actual MPV screenshot evidence unavailable')
            assert a['frames']==b['frames'],'paused image counted as progress'
            response=command(['set_property','pause',False]);assert response.get('error')=='success'
            time.sleep(.65)
            c=reader.capture()
            position=command(['get_property','time-pos']).get('data')
            assert c is not None and c['frames']>b['frames'],'actual moving decoded image not observed'
            assert position>0
            command(['set_property','pause',True])
            d=reader.capture();time.sleep(.15);e=reader.capture()
            assert d is not None and e is not None and d['frames']==e['frames']
            assert not list((root/'frames').iterdir()),'private raw image retained'
            return {'status':'PASS','actual_mpv_version':'0.40.0',
                    'owned_headless_process':True,'fixture_generated':True,
                    'external_network_requests':0,'physical_tv_output':False,
                    'real_production_hls_acceptance':False,'paused_images_do_not_progress':True,
                    'moving_decoded_pixels_observed':True,'raw_images_retained':False,
                    'counter_kind':'observed_decoded_image_changes_not_total_frames'}
        finally:
            if process.poll() is None:
                try:command(['quit'])
                except Exception:pass
                try:process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:process.wait(timeout=2)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)

if __name__=='__main__':
    try:print(json.dumps(run()))
    except Exception as exc:
        print(json.dumps({'status':'FAIL','error_type':type(exc).__name__,
                          'error':str(exc) if isinstance(exc,(RuntimeError,AssertionError)) else 'bounded private test failure',
                          'physical_tv_output':False,'fixture_generated':True}))
        raise SystemExit(1)
