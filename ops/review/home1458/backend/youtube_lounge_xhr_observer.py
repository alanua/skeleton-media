"""Observe existing receiver XHRs using native getters, without patching the page."""
from collections import Counter
import hashlib
import json
import time
from urllib.parse import urlsplit
import urllib.request
from youtube_lounge_events import LoungeStream,LoungeProtocolError,receiver_request

GROUP='skeleton_lounge_existing_xhr'
VERIFY_NATIVE_FN="""function(){return {count:this.length,native_response_url:Object.getOwnPropertyDescriptor(XMLHttpRequest.prototype,'responseURL').get.toString().includes('[native code]'),native_ready_state:Object.getOwnPropertyDescriptor(XMLHttpRequest.prototype,'readyState').get.toString().includes('[native code]'),native_response_text:Object.getOwnPropertyDescriptor(XMLHttpRequest.prototype,'responseText').get.toString().includes('[native code]')}}"""
READ_RECEIVER_FN=r"""function(){
const proto=XMLHttpRequest.prototype;
const getURL=Object.getOwnPropertyDescriptor(proto,'responseURL').get;
const getState=Object.getOwnPropertyDescriptor(proto,'readyState').get;
const getText=Object.getOwnPropertyDescriptor(proto,'responseText').get;
const result=[];
for(let i=0;i<this.length&&i<256&&result.length<16;i++){
 try{
  const x=this[i],url=Reflect.apply(getURL,x,[]);
  if(!/^https:\/\/(?:www\.)?youtube\.com\/api\/lounge\/bc\/bind(?:\?|$)/.test(url))continue;
  if(!/[?&]device=LOUNGE_SCREEN(?:&|$)/.test(url)||!/[?&]RID=rpc(?:&|$)/.test(url))continue;
  const state=Reflect.apply(getState,x,[]);
  if(state<2)continue;
  try{const text=Reflect.apply(getText,x,[]);result.push(text.length<=262144?{url,text,state}:{url,too_large:true,state});}
  catch(e){result.push({url,text_unavailable:true,state});}
 }catch(e){}
}
return result;
}"""


class SnapshotTracker:
    def __init__(self):
        self.streams={};self.stats=Counter();self.commands=Counter();self.session_high={};self.batch_primed=False
    def consume(self,snapshots):
        if not isinstance(snapshots,list) or len(snapshots)>16:raise ValueError('Invalid bounded snapshot')
        fresh=[];initial_batch=not self.batch_primed
        for snapshot in snapshots:
            if not isinstance(snapshot,dict):continue
            url=snapshot.get('url');meta=receiver_request(url)
            if not meta or not meta['receive_stream'] or meta['session_digest'] is None:continue
            self.stats['verified_receiver_snapshots']+=1
            if snapshot.get('too_large') or snapshot.get('text_unavailable'):
                self.stats['unreadable_or_oversize']+=1;continue
            text=snapshot.get('text')
            if not isinstance(text,str):continue
            raw=text.encode();key=hashlib.sha256(url.encode()).hexdigest();entry=self.streams.get(key)
            was_primed=bool(entry is not None and entry['primed'])
            reset=entry is not None and len(raw)<entry['length']
            if entry is None or len(raw)<entry['length']:
                if key not in self.streams and len(self.streams)>=64:self.stats['stream_bound_reached']+=1;continue
                entry={'parser':LoungeStream(meta['watermark']),'length':0,'primed':False,'skip_partial_frame':None};self.streams[key]=entry
            parser=entry['parser']
            if parser.failed:continue
            before=len(parser.evidence);frames=parser.frames
            try:parser.feed(raw[entry['length']:])
            except LoungeProtocolError:self.stats['stream_rejected']+=1;continue
            entry['length']=len(raw);events=parser.evidence[before:]
            session=meta['session_digest'];baseline=self.session_high.get(session,meta['watermark'])
            if initial_batch or (not entry['primed'] and (session not in self.session_high or reset)):
                self.stats['priming_media_load_records']+=len(events)
                entry['primed']=True
                if parser.expected is not None or parser.buffer:entry['skip_partial_frame']=parser.frames+1
            else:
                entry['primed']=True
                for event in events:
                    if event.frame_number==entry['skip_partial_frame']:
                        self.stats['priming_partial_records_discarded']+=1;continue
                    if baseline is not None and event.sequence>baseline:
                        self.stats['fresh_media_load_records']+=1
                        baseline=event.sequence
                        fresh.append({'sequence':event.sequence,'command':event.command,
                            'video_sha256':event.video_sha256,'session_digest':session,
                            'stream_digest':key,'observation_kind':'append' if was_primed else 'new_request'})
            if parser.watermark is not None:
                self.session_high[session]=max(self.session_high.get(session,-1),parser.watermark)
            if parser.frames>frames:self.stats['frames_decoded']+=parser.frames-frames
        self.commands=Counter()
        for entry in self.streams.values():self.commands.update(entry['parser'].commands)
        self.batch_primed=True
        return fresh
    def report(self):
        shapes=Counter()
        for entry in self.streams.values():shapes.update(entry['parser'].media_shapes)
        return {'schema':'skeleton.youtube.existing_lounge_observation.v1','counts':dict(self.stats),
            'commands':dict(self.commands),'media_shapes':dict(shapes),'receiver_wire_contract_observed':self.stats['frames_decoded']>0,
            'fresh_media_load_observed':self.stats['fresh_media_load_records']>0,
            'automatic_mode_change_enabled':False,'page_hooks_installed':False,'media_input_sent':False,
            'raw_payloads_saved':False,'priming_records_never_trigger':True}


class NativeXHRReader:
    def __init__(self,transport,event_handler=None):
        self.transport=transport;self.sequence=0;self.objects=None;self.discovered_at=0
        self.event_handler=event_handler;self.network_observation_enabled=False
    def call(self,method,params):
        if method not in {'Runtime.evaluate','Runtime.queryObjects','Runtime.callFunctionOn','Runtime.releaseObjectGroup','Network.enable','Network.disable'}:
            raise ValueError('Non-observation method refused')
        if method=='Network.enable' and params!={'maxTotalBufferSize':2097152,'maxResourceBufferSize':262144,'maxPostDataSize':0}:
            raise ValueError('Non-observation network configuration refused')
        if method=='Network.disable' and params!={}:raise ValueError('Unexpected network parameters')
        if method=='Runtime.evaluate' and params.get('expression')!='XMLHttpRequest.prototype':raise ValueError('Unknown expression refused')
        if method=='Runtime.callFunctionOn' and params.get('functionDeclaration') not in {VERIFY_NATIVE_FN,READ_RECEIVER_FN}:raise ValueError('Unknown function refused')
        self.sequence+=1;seq=self.sequence;self.transport.send(json.dumps({'id':seq,'method':method,'params':params}));deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            value=json.loads(self.transport.recv())
            if value.get('id')!=seq:
                if self.event_handler is not None:self.event_handler(value)
                continue
            if value.get('error'):raise RuntimeError('Native read failed')
            result=value.get('result') or {}
            if result.get('exceptionDetails'):raise RuntimeError('Native getter read failed')
            return result
        raise TimeoutError('Native read deadline exceeded')
    def enable_network_observation(self):
        self.call('Network.enable',{'maxTotalBufferSize':2097152,'maxResourceBufferSize':262144,'maxPostDataSize':0})
        self.network_observation_enabled=True

    def snapshot(self):
        # Reuse the active request handles; a heap discovery is not needed on every poll.
        if self.objects is None or time.monotonic()-self.discovered_at>=30:
            self.call('Runtime.releaseObjectGroup',{'objectGroup':GROUP})
            proto=self.call('Runtime.evaluate',{'expression':'XMLHttpRequest.prototype','objectGroup':GROUP,
                'returnByValue':False,'throwOnSideEffect':True,'timeout':1000})['result']['objectId']
            self.objects=self.call('Runtime.queryObjects',{'prototypeObjectId':proto,'objectGroup':GROUP})['objects']['objectId']
            self.discovered_at=time.monotonic()
        native=self.call('Runtime.callFunctionOn',{'objectId':self.objects,'functionDeclaration':VERIFY_NATIVE_FN,
            'returnByValue':True,'throwOnSideEffect':True,'objectGroup':GROUP})['result'].get('value')
        if not isinstance(native,dict) or not all(native.get(k) is True for k in ['native_response_url','native_ready_state','native_response_text']):
            raise RuntimeError('Non-native accessors refused')
        # V8 conservatively marks these DOM getters as possible side effects.
        # Only the validated native getters above are applied; there is no page callback,
        # prototype replacement, network request, DOM write or playback API in this function.
        result=self.call('Runtime.callFunctionOn',{'objectId':self.objects,'functionDeclaration':READ_RECEIVER_FN,
            'returnByValue':True,'objectGroup':GROUP})['result'].get('value')
        if not result or all(x.get('state')==4 for x in result if isinstance(x,dict)):self.objects=None
        return result
    def close(self):
        try:
            if self.network_observation_enabled:self.call('Network.disable',{})
            self.call('Runtime.releaseObjectGroup',{'objectGroup':GROUP})
        finally:self.transport.close()


def connect_reader():
    import websocket
    with urllib.request.urlopen('http://127.0.0.1:9223/json',timeout=3) as response:pages=json.load(response)
    pages=[p for p in pages if p.get('type')=='page' and urlsplit(p.get('url','')).hostname in {'youtube.com','www.youtube.com'} and urlsplit(p.get('url','')).path=='/tv']
    if len(pages)!=1:raise RuntimeError('Missing or ambiguous receiver target')
    url=pages[0].get('webSocketDebuggerUrl','');u=urlsplit(url)
    if u.scheme!='ws' or u.hostname!='127.0.0.1' or u.port!=9223 or not u.path.startswith('/devtools/page/'):raise RuntimeError('Unexpected debugger transport')
    return NativeXHRReader(websocket.create_connection(url,timeout=3,origin='http://127.0.0.1:9223'))


def observe(seconds=15):
    if not 1<=seconds<=45:raise ValueError('Observation duration outside bound')
    reader=connect_reader();tracker=SnapshotTracker();started=time.monotonic()
    try:
        while True:
            tracker.consume(reader.snapshot())
            if time.monotonic()-started>=seconds:break
            time.sleep(min(2,max(0,seconds-(time.monotonic()-started))))
    finally:reader.close()
    report=tracker.report();report['duration_seconds']=round(time.monotonic()-started,2);return report


if __name__=='__main__':
    import sys
    print(json.dumps(observe(int(sys.argv[1]) if len(sys.argv)>1 else 15)))
