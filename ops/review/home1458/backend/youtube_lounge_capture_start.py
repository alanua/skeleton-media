"""Start exactly one fixed 180-second diagnostic, with an external hard deadline."""
import json
from pathlib import Path
import subprocess
import time


def main():
    work=Path(__file__).parent;status=work/'phone-cast-capture.json';program=work/'youtube_lounge_capture.py'
    if status.exists():
        value=json.loads(status.read_text());pid=value.get('pid')
        try:
            command=(Path('/proc')/str(int(pid))/'cmdline').read_bytes().decode(errors='replace')
            active=value.get('status') in {'starting','running'} and str(program) in command and time.time()<float(value.get('deadline_at',0))+15
        except (ValueError,OSError,TypeError):active=False
        if active:print(json.dumps({'status':'already_running','pid':pid,'seconds_remaining':max(0,int(value['deadline_at']-time.time()))}));return
        archive=work/('phone-cast-capture-'+str(time.time_ns())+'.json');status.replace(archive)
    started=time.time()
    child=subprocess.Popen(['/usr/bin/timeout','--signal=TERM','--kill-after=5s','200s','/usr/bin/python3',str(program)],
        cwd=work,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        if status.exists():
            value=json.loads(status.read_text())
            if float(value.get('started_at',0))>=started and (value.get('ready_for_new_cast') or value.get('status')=='failed'):
                print(json.dumps({'status':value['status'],'pid':value['pid'],'seconds_remaining':max(0,int(value['deadline_at']-time.time())),
                    'priming_complete':value['priming_complete'],'ready_for_new_cast':value.get('ready_for_new_cast',False),'automatic_mode_change_enabled':False,'media_input_sent':False}));return
        if child.poll() is not None:raise RuntimeError('Bounded capture did not start')
        time.sleep(.1)
    print(json.dumps({'status':'starting','supervisor_pid':child.pid,'hard_deadline_seconds':200,'automatic_mode_change_enabled':False}))


if __name__=='__main__':main()
