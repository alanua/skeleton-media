import base64
import json
import unittest
from youtube_lounge_observer import LoungeObservation,ALLOWED_METHODS
from test_youtube_lounge_events import frame

URL='https://www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN&RID=rpc&AID=10&SID=private-session&loungeIdToken=private-token'


class FakeTransport:
    def __init__(self):self.sent=[]
    def send(self,text):self.sent.append(json.loads(text))


class ObserverTests(unittest.TestCase):
    def start_request(self,observer,transport,rid='request',url=URL):
        observer.process({'method':'Network.requestWillBeSent','params':{'requestId':rid,'request':{'url':url}}},transport)
        observer.process({'method':'Network.responseReceived','params':{'requestId':rid,'response':{'url':url,'status':200}}},transport)
        return transport.sent[-1]['id']
    def test_allowed_methods_have_no_media_pairing_cookie_or_page_mutation(self):
        observer=LoungeObservation();transport=FakeTransport()
        self.assertEqual(ALLOWED_METHODS,{'Network.enable','Network.disable','Network.streamResourceContent','Network.getResponseBody'})
        for name in ['Input.dispatchKeyEvent','Page.navigate','Page.reload','Runtime.evaluate','Network.replayXHR','Network.getCookies','Network.setCookie']:
            with self.assertRaises(ValueError):observer.send(transport,name,{})
        self.assertEqual(transport.sent,[])
    def test_streamed_media_event_is_private_diagnostic_only(self):
        observer=LoungeObservation();transport=FakeTransport();rpc=self.start_request(observer,transport)
        data=frame([[11,['setVideo',{'videoId':'AAAAAAAAAAA','privateToken':'do-not-export'}]]])
        observer.process({'id':rpc,'result':{'bufferedData':base64.b64encode(data).decode()}},transport)
        report=observer.report();self.assertEqual(report['media_load_candidates'],1);self.assertFalse(report['automatic_mode_change_enabled'])
        for value in ['private-session','private-token','do-not-export','AAAAAAAAAAA']:
            self.assertNotIn(value,json.dumps(report))
    def test_events_queued_before_stream_reply_keep_wire_order(self):
        observer=LoungeObservation();transport=FakeTransport();rpc=self.start_request(observer,transport)
        data=frame([[11,['setVideo',{'videoId':'AAAAAAAAAAA'}]]]);split=7
        observer.process({'method':'Network.dataReceived','params':{'requestId':'request','data':base64.b64encode(data[split:]).decode()}},transport)
        observer.process({'id':rpc,'result':{'bufferedData':base64.b64encode(data[:split]).decode()}},transport)
        self.assertEqual(observer.report()['media_load_candidates'],1)
    def test_duplicate_request_same_session_is_not_new_media_twice(self):
        observer=LoungeObservation();transport=FakeTransport();data=base64.b64encode(frame([[11,['setVideo',{'videoId':'AAAAAAAAAAA'}]]])).decode()
        for rid in ['one','two']:
            rpc=self.start_request(observer,transport,rid)
            observer.process({'id':rpc,'result':{'bufferedData':data}},transport)
        self.assertEqual(observer.report()['media_load_candidates'],1)
    def test_cached_or_mismatched_response_cannot_be_receiver_event(self):
        for response in [{'url':URL,'status':200,'fromDiskCache':True},{'url':'https://other.org','status':200},{'url':URL,'status':401}]:
            observer=LoungeObservation();transport=FakeTransport()
            observer.process({'method':'Network.requestWillBeSent','params':{'requestId':'request','request':{'url':URL}}},transport)
            observer.process({'method':'Network.responseReceived','params':{'requestId':'request','response':response}},transport)
            self.assertEqual(transport.sent,[]);self.assertEqual(observer.report()['media_load_candidates'],0)
    def test_handshake_sender_and_unknown_roles_are_not_subscribed(self):
        for url in [URL.replace('RID=rpc','RID=1'),URL.replace('LOUNGE_SCREEN','REMOTE_CONTROL'),URL.replace('device=LOUNGE_SCREEN&','')]:
            observer=LoungeObservation();transport=FakeTransport()
            observer.process({'method':'Network.requestWillBeSent','params':{'requestId':'request','request':{'url':url}}},transport)
            self.assertFalse(observer.requests);self.assertEqual(transport.sent,[])
    def test_stream_method_failure_can_fall_back_only_to_finished_body(self):
        observer=LoungeObservation();transport=FakeTransport();rpc=self.start_request(observer,transport)
        observer.process({'method':'Network.loadingFinished','params':{'requestId':'request'}},transport)
        observer.process({'id':rpc,'error':{'message':'not available'}},transport)
        self.assertEqual(transport.sent[-1]['method'],'Network.getResponseBody')
        body=frame([[11,['noop']]]).decode()
        observer.process({'id':transport.sent[-1]['id'],'result':{'body':body,'base64Encoded':False}},transport)
        self.assertEqual(observer.report()['commands'],{'noop':1});self.assertFalse(observer.requests)
    def test_untrusted_redirect_discards_old_request(self):
        observer=LoungeObservation();transport=FakeTransport();self.start_request(observer,transport)
        observer.process({'method':'Network.requestWillBeSent','params':{'requestId':'request','request':{'url':'https://other.org'}}},transport)
        self.assertFalse(observer.requests)
    def test_missing_session_cannot_be_media_candidate(self):
        observer=LoungeObservation();transport=FakeTransport();rpc=self.start_request(observer,transport,url=URL.replace('&SID=private-session',''))
        observer.process({'id':rpc,'result':{'bufferedData':base64.b64encode(frame([[11,['setVideo',{'videoId':'AAAAAAAAAAA'}]]])).decode()}},transport)
        self.assertEqual(observer.report()['media_load_candidates'],0)
    def test_queue_memory_bound_fails_closed(self):
        observer=LoungeObservation();transport=FakeTransport();self.start_request(observer,transport)
        for _ in range(3):observer.process({'method':'Network.dataReceived','params':{'requestId':'request','data':'A'*180000}},transport)
        record=observer.requests['request'];self.assertTrue(record['rejected']);self.assertEqual(record['queued'],[])
    def test_enable_failure_is_not_reported_as_active_observation(self):
        observer=LoungeObservation();transport=FakeTransport();observer.enable_request_id=observer.send(transport,'Network.enable')
        observer.process({'id':observer.enable_request_id,'error':{'message':'not supported'}},transport)
        self.assertFalse(observer.report()['network_tracking_enabled']);self.assertFalse(observer.report()['receiver_channel_observed'])
    def test_transport_counts_do_not_export_urls_or_payloads(self):
        observer=LoungeObservation();transport=FakeTransport()
        observer.process({'method':'Network.webSocketCreated','params':{'requestId':'x','url':'wss://www.youtube.com/api/lounge/bc/bind?token=private-token'}},transport)
        observer.process({'method':'Network.webSocketFrameReceived','params':{'requestId':'x','response':{'payloadData':'private-payload'}}},transport)
        report=observer.report();self.assertEqual(report['counts']['lounge_websocket_connections'],1)
        self.assertNotIn('private',json.dumps(report));self.assertFalse(report['receiver_channel_observed']);self.assertEqual(transport.sent,[])


if __name__=='__main__':unittest.main()
