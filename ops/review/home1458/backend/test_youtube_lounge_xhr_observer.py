import json
import unittest
from youtube_lounge_events import MediaLoadEvidence
from youtube_lounge_xhr_observer import SnapshotTracker,NativeXHRReader,READ_RECEIVER_FN,VERIFY_NATIVE_FN
from test_youtube_lounge_events import frame

URL='https://www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN&RID=rpc&AID=0&SID=private-session&loungeIdToken=private-token'


def snapshot(wire,url=URL):return {'url':url,'text':wire.decode(),'state':3}
def load(seq,video='AAAAAAAAAAA'):return [seq,['setVideo',{'videoId':video}]]


class TrackerTests(unittest.TestCase):
    def test_existing_media_is_priming_not_new_cast(self):
        t=SnapshotTracker();t.consume([snapshot(frame([load(1)]))]);r=t.report()
        self.assertEqual(r['counts']['priming_media_load_records'],1);self.assertFalse(r['fresh_media_load_observed'])
        self.assertTrue(r['receiver_wire_contract_observed']);self.assertFalse(r['automatic_mode_change_enabled'])
    def test_new_appended_command_is_separate_from_priming(self):
        t=SnapshotTracker();old=frame([load(1)]);t.consume([snapshot(old)])
        t.consume([snapshot(old+frame([load(2,'BBBBBBBBBBB')]))]);r=t.report()
        self.assertEqual(r['counts']['fresh_media_load_records'],1)
        for private in ['private-session','private-token','AAAAAAAAAAA','BBBBBBBBBBB']:
            self.assertNotIn(private,json.dumps(r))
    def test_repeated_snapshot_and_request_replay_are_not_new(self):
        t=SnapshotTracker();old=frame([load(1)]);t.consume([snapshot(old)]);t.consume([snapshot(old)]);t.consume([snapshot(old)])
        self.assertFalse(t.report()['fresh_media_load_observed'])
    def test_partial_priming_frame_completion_is_not_new(self):
        t=SnapshotTracker();old=frame([load(1)]);partial=frame([load(2)]);t.consume([snapshot(old+partial[:10])])
        t.consume([snapshot(old+partial+frame([load(3)]))]);r=t.report()
        self.assertEqual(r['counts']['priming_partial_records_discarded'],1)
        self.assertEqual(r['counts']['fresh_media_load_records'],1)
    def test_unknown_role_session_or_baseline_never_creates_cast(self):
        for url in [URL.replace('LOUNGE_SCREEN','REMOTE_CONTROL'),URL.replace('&SID=private-session',''),URL.replace('&AID=0','')]:
            t=SnapshotTracker();t.consume([snapshot(frame([load(1)]),url)]);t.consume([snapshot(frame([load(1),load(2)]),url)])
            self.assertFalse(t.report()['fresh_media_load_observed'])
    def test_response_reset_restarts_priming(self):
        t=SnapshotTracker();t.consume([snapshot(frame([load(1),load(2)]))]);t.consume([snapshot(frame([load(3)]))])
        self.assertFalse(t.report()['fresh_media_load_observed'])
    def test_new_request_in_primed_session_keeps_global_sequence_fence(self):
        t=SnapshotTracker();old=frame([load(1)]);t.consume([snapshot(old)])
        t.consume([snapshot(old+frame([load(2)]),URL+'&zx=next')])
        self.assertEqual(t.report()['counts']['fresh_media_load_records'],1)
    def test_invalid_json_cannot_later_recover_into_forged_event(self):
        t=SnapshotTracker();t.consume([snapshot(b'4\nnope')]);t.consume([snapshot(b'4\nnope'+frame([load(1)]))])
        self.assertFalse(t.report()['fresh_media_load_observed']);self.assertEqual(t.report()['counts']['stream_rejected'],1)
    def test_bounds_and_unavailable_text_are_safe(self):
        t=SnapshotTracker();t.consume([{'url':URL,'too_large':True},{'url':URL,'text_unavailable':True}])
        self.assertEqual(t.report()['counts']['unreadable_or_oversize'],2)
        with self.assertRaises(ValueError):t.consume([{}]*17)


class FakeTransport:
    def __init__(self,native=True):self.sent=[];self.native=native;self.closed=False
    def send(self,text):self.sent.append(json.loads(text))
    def recv(self):
        req=self.sent[-1];method=req['method'];params=req['params']
        if method=='Runtime.evaluate':result={'result':{'objectId':'prototype'}}
        elif method=='Runtime.queryObjects':result={'objects':{'objectId':'array'}}
        elif method=='Runtime.callFunctionOn':
            value={'native_response_url':self.native,'native_ready_state':self.native,'native_response_text':self.native} if params['functionDeclaration']==VERIFY_NATIVE_FN else []
            result={'result':{'value':value}}
        else:result={}
        return json.dumps({'id':req['id'],'result':result})
    def close(self):self.closed=True


class ReaderTests(unittest.TestCase):
    def test_methods_and_function_bodies_are_allowlisted(self):
        c=NativeXHRReader(FakeTransport())
        for method in ['Page.reload','Input.dispatchKeyEvent','Network.replayXHR']:
            with self.assertRaises(ValueError):c.call(method,{})
        with self.assertRaises(ValueError):c.call('Runtime.evaluate',{'expression':'video.play()'})
        with self.assertRaises(ValueError):c.call('Runtime.callFunctionOn',{'functionDeclaration':'function(){location.reload()}'})
    def test_snapshot_only_reads_validated_native_getters(self):
        transport=FakeTransport();reader=NativeXHRReader(transport);self.assertEqual(reader.snapshot(),[]);reader.close()
        self.assertTrue(transport.closed);self.assertEqual(transport.sent[-1]['method'],'Runtime.releaseObjectGroup')
        calls=[x for x in transport.sent if x['method']=='Runtime.callFunctionOn'];self.assertEqual([x['params']['functionDeclaration'] for x in calls],[VERIFY_NATIVE_FN,READ_RECEIVER_FN])
    def test_non_native_getter_refuses_payload_read(self):
        transport=FakeTransport(native=False);reader=NativeXHRReader(transport)
        with self.assertRaises(RuntimeError):reader.snapshot()
        self.assertFalse(any(x['params'].get('functionDeclaration')==READ_RECEIVER_FN for x in transport.sent))


if __name__=='__main__':unittest.main()
