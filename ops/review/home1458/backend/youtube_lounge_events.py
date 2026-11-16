"""Bounded passive Lounge wire decoder. Never sends commands or changes modes.

Wire contract reference: aykevl/plaincast apps/youtube/youtube.go,
handleMessageStream/handleRawReceivedMessage and setPlaylist/setVideo handlers.
No third-party receiver implementation is copied or run.
"""
from dataclasses import dataclass
from collections import Counter
import hashlib
import json
import math
import re
from urllib.parse import parse_qs,urlsplit

_VIDEO=re.compile(r'^[A-Za-z0-9_-]{11}$')
_KNOWN={'c','S','noop','remoteConnected','remoteDisconnected','loungeStatus',
        'setPlaylist','setVideo','updatePlaylist','getNowPlaying','getPlaylist',
        'play','pause','seekTo','setVolume','getVolume','stop','onStateChange',
        'nowPlaying','onVolumeChanged','onAdStateChange','onSubtitlesTrackChanged'}


class LoungeProtocolError(ValueError):pass


@dataclass(frozen=True)
class MediaLoadEvidence:
    sequence:int
    command:str
    video_sha256:str
    frame_number:int=0


def receiver_request(url):
    """Return only non-secret receiver role and acknowledged sequence metadata."""
    if not isinstance(url,str) or len(url)>32768:return None
    try:
        u=urlsplit(url)
        if u.scheme!='https' or u.hostname not in {'www.youtube.com','youtube.com'} or u.port not in (None,443):return None
        if u.username is not None or u.password is not None or u.fragment or u.path!='/api/lounge/bc/bind':return None
        q=parse_qs(u.query,keep_blank_values=True,max_num_fields=100)
        if q.get('device')!=['LOUNGE_SCREEN']:return None
        ack=q.get('AID')
        watermark=None
        if ack is not None:
            if len(ack)!=1 or not re.fullmatch(r'-?\d{1,12}',ack[0]):return None
            watermark=int(ack[0])
            if watermark< -1:return None
        # Session values are hashed together; neither token nor raw session escapes.
        sid=q.get('SID')
        session=None
        if sid is not None:
            if len(sid)!=1 or not sid[0] or len(sid[0])>4096:return None
            session=hashlib.sha256(sid[0].encode()).hexdigest()
        return {'role':'LOUNGE_SCREEN','watermark':watermark,'session_digest':session,'receive_stream':q.get('RID')==['rpc']}
    except (ValueError,UnicodeError):return None


def _video_id(command,args):
    if command not in {'setPlaylist','setVideo'} or not isinstance(args,dict):return None
    if 'currentTime' in args:
        value=args['currentTime']
        if isinstance(value,bool):return None
        try:value=float(value)
        except (TypeError,ValueError,OverflowError):return None
        if not math.isfinite(value) or value<0 or value>604800:return None
    selected=args.get('videoId')
    if 'videoIds' in args:
        raw=args['videoIds'];index=args.get('currentIndex')
        if not isinstance(raw,str) or len(raw)>1200:return None
        values=raw.split(',')
        if not values or len(values)>100 or not all(_VIDEO.fullmatch(x) for x in values):return None
        if not isinstance(index,str) or not re.fullmatch(r'\d{1,3}',index):return None
        index=int(index)
        if index>=len(values):return None
        if selected is not None and selected!=values[index]:return None
        selected=values[index]
    return selected if isinstance(selected,str) and _VIDEO.fullmatch(selected) else None


class LoungeStream:
    def __init__(self,watermark=None,max_frame=65536,max_total=262144):
        if watermark is not None and (type(watermark) is not int or watermark< -1):raise ValueError('Invalid watermark')
        self.watermark=watermark;self.max_frame=max_frame;self.max_total=max_total
        self.buffer=b'';self.expected=None;self.total=0;self.failed=False
        self.frames=0;self.commands={};self.replayed=0;self.evidence=[];self.malformed=0
        self.media_shapes=Counter()
    def feed(self,chunk):
        if self.failed:raise LoungeProtocolError('Stream already rejected')
        if not isinstance(chunk,bytes):raise LoungeProtocolError('Expected bytes')
        self.total+=len(chunk)
        if self.total>self.max_total:self._reject('Stream exceeds observation bound')
        self.buffer+=chunk
        while True:
            if self.expected is None:
                if b'\n' not in self.buffer:
                    if len(self.buffer)>8:self._reject('Invalid length prefix')
                    return
                prefix,self.buffer=self.buffer.split(b'\n',1)
                if not re.fullmatch(rb'\d{1,7}',prefix):self._reject('Invalid length prefix')
                self.expected=int(prefix)
                if self.expected<=0 or self.expected>self.max_frame:self._reject('Invalid frame size')
            if len(self.buffer)<self.expected:return
            body,self.buffer=self.buffer[:self.expected],self.buffer[self.expected:];self.expected=None
            try:messages=json.loads(body)
            except (ValueError,UnicodeError,RecursionError):self._reject('Invalid frame JSON')
            if not isinstance(messages,list) or len(messages)>256:self._reject('Invalid message batch')
            self.frames+=1
            for message in messages:self._message(message)
    def _reject(self,reason):
        self.failed=True;self.buffer=b'';raise LoungeProtocolError(reason)
    def _message(self,message):
        if not isinstance(message,list) or len(message)!=2 or type(message[0]) is not int or not 0<=message[0]<=2**53-1:
            self.malformed+=1;return
        sequence,payload=message
        if not isinstance(payload,list) or not payload or not isinstance(payload[0],str):self.malformed+=1;return
        name=payload[0];safe_name=name if name in _KNOWN else 'other'
        self.commands[safe_name]=self.commands.get(safe_name,0)+1
        if name in {'setPlaylist','setVideo'}:
            args=payload[1] if len(payload)==2 and isinstance(payload[1],dict) else None
            self.media_shapes[name+':args:'+('map' if args is not None else 'other')]+=1
            if args is not None:
                for key in ['videoId','videoIds','currentIndex','currentTime']:
                    value=args.get(key)
                    kind='missing' if key not in args else ('str' if isinstance(value,str) else 'bool' if isinstance(value,bool) else 'int' if type(value) is int else 'float' if type(value) is float else 'other')
                    self.media_shapes[name+':'+key+':'+kind]+=1
                if args.get('currentTime') in (-1,'-1'):self.media_shapes[name+':live_edge_time_sentinel']+=1
        if self.watermark is not None and sequence<=self.watermark:self.replayed+=1;return
        # A request without a verified AID is observational only, never new-media evidence.
        known_baseline=self.watermark is not None
        self.watermark=sequence if known_baseline else None
        video=_video_id(name,payload[1] if len(payload)==2 else None)
        if known_baseline and video:
            self.evidence.append(MediaLoadEvidence(sequence,name,hashlib.sha256(video.encode()).hexdigest(),self.frames))
    def summary(self):
        return {'frames':self.frames,'commands':dict(self.commands),'replayed':self.replayed,
            'malformed':self.malformed,'media_load_count':len(self.evidence),'failed':self.failed,
            'baseline_known':self.watermark is not None,'media_shapes':dict(self.media_shapes)}
