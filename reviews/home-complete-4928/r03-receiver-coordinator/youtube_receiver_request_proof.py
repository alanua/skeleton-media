"""Read-only proof for newly created native receiver XHRs; no payload retention."""
import hashlib
import time
from youtube_lounge_events import receiver_request


class ReceiverRequestProofs:
    def __init__(self,receiver_epoch,clock=time.monotonic):
        self.receiver_epoch=receiver_epoch;self.clock=clock;self.requests={}
    def event(self,message):
        now=self.clock()
        self.requests={k:v for k,v in self.requests.items() if 0<=now-v['observed_at']<=20}
        method=message.get('method');p=message.get('params') or {};key=p.get('requestId')
        if not isinstance(key,str):return
        if method=='Network.requestWillBeSent':
            self.requests.pop(key,None)
            url=(p.get('request') or {}).get('url');meta=receiver_request(url)
            if (not meta or not meta['receive_stream'] or not meta['session_digest']
                    or meta['watermark'] is None or len(self.requests)>=64):return
            self.requests[key]=dict(stream_digest=hashlib.sha256(url.encode()).hexdigest(),
                session_digest=meta['session_digest'],receiver_epoch=self.receiver_epoch,
                observed_at=now,verified_response=False)
        elif method=='Network.responseReceived' and key in self.requests:
            record=self.requests[key];response=p.get('response') or {}
            url=response.get('url');meta=receiver_request(url)
            if (not meta or response.get('status')!=200 or response.get('fromDiskCache')
                    or response.get('fromServiceWorker')
                    or hashlib.sha256(url.encode()).hexdigest()!=record['stream_digest']
                    or meta['session_digest']!=record['session_digest']):
                self.requests.pop(key,None);return
            record['verified_response']=True
        elif method in ('Network.loadingFailed','Network.requestServedFromCache'):
            self.requests.pop(key,None)
    def snapshot(self):
        now=self.clock()
        return {v['stream_digest']:dict(v) for v in self.requests.values()
                if v['verified_response'] and 0<=now-v['observed_at']<=12}
