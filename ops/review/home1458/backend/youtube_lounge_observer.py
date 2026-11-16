"""Short passive CDP observation; no pairing, new receiver or media actuation."""
import base64
from collections import Counter
import json
import time
from urllib.parse import urlsplit
import urllib.request
from youtube_lounge_events import LoungeStream,LoungeProtocolError,receiver_request

ALLOWED_METHODS=frozenset({'Network.enable','Network.disable','Network.streamResourceContent','Network.getResponseBody'})


class LoungeObservation:
    def __init__(self):
        self.requests={};self.pending={};self.stats=Counter();self.commands=Counter();self.sequence=0
        self.session_watermarks={};self.media_load_candidates=0
        self.enable_request_id=None;self.network_enabled=False
    def send(self,transport,method,params=None):
        if method not in ALLOWED_METHODS:raise ValueError('Non-observation CDP method refused')
        self.sequence+=1;transport.send(json.dumps({'id':self.sequence,'method':method,'params':params or {}}));return self.sequence
    def _feed(self,record,data):
        if not data:return
        try:
            chunk=base64.b64decode(data,validate=True)
            if len(chunk)>262144:raise ValueError('Bound exceeded')
            parser=record['parser'];before=len(parser.evidence);parser.feed(chunk)
            for evidence in parser.evidence[before:]:
                session=record['meta']['session_digest']
                if not session:continue
                previous=self.session_watermarks.get(session,record['meta']['watermark'])
                if previous is not None and evidence.sequence>previous:
                    self.media_load_candidates+=1
                    self.session_watermarks[session]=evidence.sequence
        except (ValueError,LoungeProtocolError):
            self.stats['stream_rejected']+=1;record['rejected']=True
    def _finish(self,request_id):
        record=self.requests.pop(request_id,None)
        if record:
            summary=record['parser'].summary();self.commands.update(summary['commands'])
            self.stats['frames']+=summary['frames'];self.stats['replayed_messages']+=summary['replayed']
            self.stats['malformed_messages']+=summary['malformed']
    def process(self,message,transport):
        if 'id' in message:
            if message['id']==self.enable_request_id:
                self.network_enabled=not bool(message.get('error'))
                if not self.network_enabled:self.stats['network_enable_errors']+=1
                return
            pending=self.pending.pop(message['id'],None)
            if not pending:return
            action,request_id=pending;record=self.requests.get(request_id)
            if not record:return
            if message.get('error'):
                self.stats['cdp_read_errors']+=1;record['stream_pending']=False
                if record.get('finished') and action=='stream':self._body(request_id,transport)
                elif action=='body':self._finish(request_id)
                return
            result=message.get('result') or {}
            if action=='stream':
                record['stream_pending']=False;record['streamed']=True
                self._feed(record,result.get('bufferedData',''))
                for data in record.pop('queued',[]):
                    if not record['rejected']:self._feed(record,data)
            else:
                body=result.get('body','')
                if not isinstance(body,str) or len(body)>262144:record['rejected']=True
                else:self._feed(record,body if result.get('base64Encoded') else base64.b64encode(body.encode()).decode())
            if record.get('finished'):self._finish(request_id)
            return
        method=message.get('method');params=message.get('params') or {};request_id=params.get('requestId')
        if isinstance(method,str) and method.startswith('Network.'):
            self.stats['network_events']+=1
        if method in {'Network.webSocketCreated','Network.webSocketFrameReceived'}:
            self.stats['websocket_events']+=1
            if method=='Network.webSocketCreated':
                try:
                    u=urlsplit(params.get('url',''))
                    if u.hostname in {'youtube.com','www.youtube.com'} and u.path.startswith('/api/lounge/'):
                        self.stats['lounge_websocket_connections']+=1
                except ValueError:pass
        if method=='Network.requestWillBeSent':
            req=params.get('request') or {};url=req.get('url','')
            try:
                parsed=urlsplit(url)
                self.stats['http_request_events']+=1
                if parsed.scheme=='https' and parsed.hostname in {'www.youtube.com','youtube.com'}:
                    self.stats['youtube_https_requests']+=1
                    if parsed.path.startswith('/api/lounge/'):self.stats['lounge_api_requests']+=1
                is_lounge=parsed.scheme=='https' and parsed.hostname in {'www.youtube.com','youtube.com'} and parsed.path=='/api/lounge/bc/bind'
            except ValueError:is_lounge=False
            if not is_lounge:
                if request_id in self.requests:self._finish(request_id)
                return
            self.stats['lounge_requests']+=1;meta=receiver_request(url)
            if not meta:
                self.stats['unverified_role_requests']+=1;return
            if not meta['receive_stream']:
                self.stats['receiver_send_or_handshake_requests']+=1;return
            self.stats['receiver_stream_requests']+=1
            if not isinstance(request_id,str) or len(self.requests)>=16 or self.stats['receiver_stream_requests']>64:return
            if request_id in self.requests:self._finish(request_id)
            self.requests[request_id]={'meta':meta,'parser':LoungeStream(meta['watermark']),
                'stream_pending':False,'streamed':False,'rejected':False,'finished':False,'queued':[],'queued_size':0}
        elif method=='Network.responseReceived' and request_id in self.requests:
            record=self.requests[request_id];response=params.get('response') or {}
            if response.get('status')!=200 or response.get('fromDiskCache') or response.get('fromServiceWorker'):
                self.stats['untrusted_or_failed_responses']+=1;self._finish(request_id);return
            # The response URL must independently describe this same trusted stream.
            meta=receiver_request(response.get('url',''))
            if meta!=record['meta']:
                self.stats['response_identity_mismatch']+=1;self._finish(request_id);return
            record['stream_pending']=True
            self.pending[self.send(transport,'Network.streamResourceContent',{'requestId':request_id})]=('stream',request_id)
        elif method=='Network.dataReceived' and request_id in self.requests:
            record=self.requests[request_id];data=params.get('data')
            if not isinstance(data,str) or record['rejected']:return
            if len(data)>349528:record['rejected']=True;self.stats['stream_rejected']+=1;return
            if record['stream_pending']:
                record['queued_size']+=len(data)
                if len(record['queued'])>=128 or record['queued_size']>349528:
                    record['rejected']=True;record['queued']=[];self.stats['stream_rejected']+=1
                else:record['queued'].append(data)
            elif record['streamed']:self._feed(record,data)
        elif method in {'Network.loadingFinished','Network.loadingFailed'} and request_id in self.requests:
            record=self.requests[request_id];record['finished']=True
            if method.endswith('Failed'):self.stats['failed_requests']+=1;self._finish(request_id)
            elif record['stream_pending']:return
            elif record['streamed']:self._finish(request_id)
            else:self._body(request_id,transport)
    def _body(self,request_id,transport):
        self.pending[self.send(transport,'Network.getResponseBody',{'requestId':request_id})]=('body',request_id)
    def report(self):
        stats=Counter(self.stats);commands=Counter(self.commands)
        for record in self.requests.values():
            summary=record['parser'].summary();stats['frames']+=summary['frames'];commands.update(summary['commands'])
        return {'schema':'skeleton.youtube.lounge_observation.v1','counts':dict(stats),
            'commands':dict(commands),'media_load_candidates':self.media_load_candidates,
            'receiver_channel_observed':stats['frames']>0,'automatic_mode_change_enabled':False,
            'network_tracking_enabled':self.network_enabled,
            'media_input_sent':False,'raw_payloads_saved':False,'status':'observation_only'}


def observe(seconds=25):
    if not 1<=seconds<=45:raise ValueError('Observation duration outside bound')
    import websocket
    with urllib.request.urlopen('http://127.0.0.1:9223/json',timeout=3) as response:pages=json.load(response)
    matches=[]
    for page in pages:
        if page.get('type')!='page':continue
        try:u=urlsplit(page.get('url',''))
        except ValueError:continue
        if u.scheme=='https' and u.hostname in {'youtube.com','www.youtube.com'} and u.path=='/tv':matches.append(page)
    if len(matches)!=1:raise RuntimeError('Receiver target is missing or ambiguous')
    ws_url=matches[0].get('webSocketDebuggerUrl','');u=urlsplit(ws_url)
    if u.scheme!='ws' or u.hostname!='127.0.0.1' or u.port!=9223 or not u.path.startswith('/devtools/page/'):
        raise RuntimeError('Unexpected receiver debugging transport')
    transport=websocket.create_connection(ws_url,timeout=2,origin='http://127.0.0.1:9223');transport.settimeout(.25)
    observation=LoungeObservation();started=time.monotonic()
    try:
        observation.enable_request_id=observation.send(transport,'Network.enable',{'maxTotalBufferSize':2097152,'maxResourceBufferSize':262144,'maxPostDataSize':0})
        while time.monotonic()-started<seconds:
            try:message=json.loads(transport.recv())
            except websocket.WebSocketTimeoutException:continue
            observation.process(message,transport)
    finally:
        try:observation.send(transport,'Network.disable')
        finally:transport.close()
    report=observation.report();report['duration_seconds']=round(time.monotonic()-started,2);return report


if __name__=='__main__':
    import sys
    print(json.dumps(observe(int(sys.argv[1]) if len(sys.argv)>1 else 25)))
