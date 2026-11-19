import hashlib
import json
import unittest
from youtube_receiver_request_proof import ReceiverRequestProofs
from youtube_lounge_xhr_observer import NativeXHRReader
from test_youtube_lounge_xhr_observer import FakeTransport,URL

class RequestProofTests(unittest.TestCase):
    def setUp(self):self.now=100.;self.proofs=ReceiverRequestProofs('a'*64,clock=lambda:self.now)
    def request(self,url=URL):
        self.proofs.event({'method':'Network.requestWillBeSent','params':{'requestId':'r','request':{'url':url,'postData':'not retained'}}})
    def response(self,**extra):
        self.proofs.event({'method':'Network.responseReceived','params':{'requestId':'r','response':dict(url=URL,status=200,**extra)}})
    def test_only_new_verified_matching_native_receiver_response_yields_proof(self):
        self.assertEqual(self.proofs.snapshot(),{})
        self.request();self.assertEqual(self.proofs.snapshot(),{})
        self.response();proof=list(self.proofs.snapshot().values())[0]
        self.assertEqual(proof['receiver_epoch'],'a'*64)
        self.assertTrue(proof['verified_response'])
        for secret in ('private-token','private-session','postData','youtube.com'):
            self.assertNotIn(secret,json.dumps(self.proofs.requests))
    def test_untrusted_role_missing_session_or_aid_does_not_create_proof(self):
        for url in [URL.replace('LOUNGE_SCREEN','REMOTE_CONTROL'),URL.replace('&AID=0',''),URL.replace('&SID=private-session','')]:
            self.request(url);self.assertEqual(self.proofs.requests,{})
    def test_failed_cached_or_mismatched_response_is_refused(self):
        for extra in ({'fromDiskCache':True},{'fromServiceWorker':True}):
            self.request();self.response(**extra);self.assertEqual(self.proofs.snapshot(),{})
        self.request()
        self.proofs.event({'method':'Network.responseReceived','params':{'requestId':'r','response':{'url':URL+'&changed=1','status':200}}})
        self.assertEqual(self.proofs.snapshot(),{})
    def test_expiration_and_failed_request_never_leave_reusable_proof(self):
        self.request();self.response();self.now+=13
        self.assertEqual(self.proofs.snapshot(),{})
        self.now=100.;self.request();self.response()
        self.proofs.event({'method':'Network.loadingFailed','params':{'requestId':'r'}})
        self.assertEqual(self.proofs.snapshot(),{})
    def test_request_count_has_a_bound(self):
        for i in range(100):
            self.proofs.event({'method':'Network.requestWillBeSent','params':{'requestId':str(i),'request':{'url':URL+'&i='+str(i)}}})
        self.assertEqual(len(self.proofs.requests),64)

class EventTransport(FakeTransport):
    def __init__(self):
        super().__init__();self.event_pending=False
    def send(self,text):
        super().send(text);self.event_pending=True
    def recv(self):
        if self.event_pending:
            self.event_pending=False
            return json.dumps({'method':'Network.requestWillBeSent','params':{'requestId':'r','request':{'url':URL}}})
        return super().recv()

class ReaderNetworkTests(unittest.TestCase):
    def test_unmatched_network_events_are_delivered_to_read_only_proof_collector(self):
        events=[];reader=NativeXHRReader(EventTransport(),event_handler=events.append)
        reader.enable_network_observation()
        self.assertTrue(events);self.assertTrue(reader.network_observation_enabled)
        reader.close()
        self.assertTrue(any(x['method']=='Network.disable' for x in reader.transport.sent))
    def test_network_observation_cannot_capture_post_data_or_send_requests(self):
        reader=NativeXHRReader(FakeTransport())
        for method,params in [('Network.enable',{'maxPostDataSize':1024}),
                              ('Network.disable',{'unexpected':True}),
                              ('Network.replayXHR',{})]:
            with self.assertRaises(ValueError):reader.call(method,params)
    def test_existing_reader_without_event_handler_keeps_prior_contract(self):
        reader=NativeXHRReader(FakeTransport());self.assertEqual(reader.snapshot(),[])
        reader.close();self.assertFalse(any(x['method'].startswith('Network.') for x in reader.transport.sent))

if __name__=='__main__':unittest.main()
