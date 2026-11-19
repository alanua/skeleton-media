"""A single bounded diagnostic capture for an operator's real phone-cast test."""
import json
import os
from pathlib import Path
import time
from youtube_lounge_xhr_observer import SnapshotTracker,connect_reader


def atomic(path,data):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,indent=2)+'\n');tmp.chmod(0o600);tmp.replace(path)


def capture(path,seconds=180,reader_factory=connect_reader,clock=time.monotonic,sleep=time.sleep):
    if not 1<=seconds<=180:raise ValueError('Capture duration outside bound')
    tracker=SnapshotTracker();reader=None;started=clock()
    result={'schema':'skeleton.youtube.phone_cast_capture.v1','status':'starting','pid':os.getpid(),
        'started_at':time.time(),'deadline_at':time.time()+seconds,'priming_complete':False,
        'automatic_mode_change_enabled':False,'media_input_sent':False,'page_hooks_installed':False}
    try:
        reader=reader_factory()
        while True:
            tracker.consume(reader.snapshot());report=tracker.report()
            result.update({'status':'running','priming_complete':True,'ready_for_new_cast':report['receiver_wire_contract_observed'],'observation':report})
            atomic(path,result)
            if tracker.report()['fresh_media_load_observed']:
                result['status']='fresh_media_load_observed';break
            if clock()-started>=seconds:
                result['status']='completed_without_fresh_media_load';break
            sleep(min(2,max(0,seconds-(clock()-started))))
    except Exception as error:
        result.update({'status':'failed','error_type':type(error).__name__})
    finally:
        if reader:
            try:reader.close()
            except Exception as error:result['cleanup_error_type']=type(error).__name__
        result['finished_at']=time.time();result['elapsed_seconds']=round(clock()-started,2)
        result['observation']=tracker.report();atomic(path,result)
    return result


if __name__=='__main__':
    import fcntl
    work=Path(__file__).parent
    with (work/'phone-cast-capture.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        capture(work/'phone-cast-capture.json')
