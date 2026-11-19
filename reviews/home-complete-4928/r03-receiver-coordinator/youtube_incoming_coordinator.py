"""Serialized fresh receiver-load coordinator; explicit bounded canonical adapters.

No worker, timer, page hook, account change or media command is created here.
The existing observer supplies evidence after priming and a stable context window.
"""
from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

DIGEST=re.compile(r'^[a-f0-9]{64}$')
MODES={'kiosk','mpv','tv','chrome','games','off'}


def digest(value):
    return isinstance(value,str) and DIGEST.fullmatch(value) is not None


def context_valid(value):
    return (isinstance(value,dict) and value.get('endpoint')=='home_edge_01'
            and digest(value.get('receiver_epoch')) and digest(value.get('mode_token'))
            and value.get('mode') in MODES and type(value.get('revision')) is int
            and value['revision']>=0 and value.get('transition') is False)


def matches(status,expected):
    if not isinstance(status,dict):return False
    video=status.get('video_id')
    return (isinstance(video,str) and re.fullmatch(r'[A-Za-z0-9_-]{11}',video)
            and hashlib.sha256(video.encode()).hexdigest()==expected
            and status.get('running') is True and status.get('pause') is False
            and type(status.get('player_state')) is int and status['player_state'] in (1,3))


class ReceiverEventLedger:
    """Reserve before actuation. Failed/uncertain handoffs never auto-replay."""
    def __init__(self,path):self.path=Path(path)
    def reserve(self,receiver,session,sequence):
        if not digest(receiver) or not digest(session) or type(sequence) is not int or not 0<=sequence<=2**53-1:
            return 'invalid'
        connection=None
        try:
            self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            if (self.path.parent.is_symlink() or self.path.parent.stat().st_uid!=os.getuid()
                    or self.path.parent.stat().st_mode & 0o077):return 'ledger_unavailable'
            if not self.path.exists():
                fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600);os.close(fd)
            if (self.path.is_symlink() or self.path.stat().st_uid!=os.getuid()
                    or self.path.stat().st_mode & 0o077):return 'ledger_unavailable'
            connection=sqlite3.connect(self.path,timeout=.25)
            connection.execute('PRAGMA busy_timeout=250')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('CREATE TABLE IF NOT EXISTS receiver_events(receiver TEXT,session TEXT,sequence INTEGER,PRIMARY KEY(receiver,session))')
            connection.execute('BEGIN IMMEDIATE')
            row=connection.execute('SELECT sequence FROM receiver_events WHERE receiver=? AND session=?',(receiver,session)).fetchone()
            if row is not None:
                if type(row[0]) is not int or not 0<=row[0]<=2**53-1:return 'ledger_unavailable'
                if sequence<=row[0]:return 'duplicate'
            elif connection.execute('SELECT COUNT(*) FROM receiver_events').fetchone()[0]>=128:
                return 'ledger_capacity'
            connection.execute('INSERT OR REPLACE INTO receiver_events VALUES(?,?,?)',(receiver,session,sequence))
            connection.commit();return 'reserved'
        except Exception:return 'ledger_unavailable'
        finally:
            if connection is not None:connection.close()


class IncomingYouTubeCoordinator:
    def __init__(self,*,ledger,context,status,handoff,verify,publish,
                 clock=time.monotonic,sleep=time.sleep):
        self.ledger=ledger;self.context=context;self.status=status
        self.handoff=handoff;self.verify=verify;self.publish=publish
        self.clock=clock;self.sleep=sleep;self._lock=threading.Lock()

    def handle(self,events,*,window_context,window_started,request_proofs=None):
        if not self._lock.acquire(blocking=False):return {'state':'busy'}
        sent=False
        try:
            now=self.clock()
            if (not context_valid(window_context)
                    or self.context()!=window_context
                    or isinstance(window_started,bool)
                    or not isinstance(window_started,(int,float))
                    or not math.isfinite(window_started) or not 0<=now-window_started<=12
                    or not isinstance(events,list) or len(events)>16):
                return {'state':'stale_context'}
            candidates=[]
            for event in events:
                if (not isinstance(event,dict) or event.get('command') not in ('setPlaylist','setVideo')
                        or not digest(event.get('video_sha256')) or not digest(event.get('session_digest'))
                        or not digest(event.get('stream_digest'))
                        or type(event.get('sequence')) is not int
                        or not 0<=event['sequence']<=2**53-1):continue
                kind=event.get('observation_kind')
                if kind=='new_request':
                    proof=(request_proofs or {}).get(event['stream_digest'])
                    if (not isinstance(proof,dict) or proof.get('verified_response') is not True
                            or proof.get('session_digest')!=event['session_digest']
                            or proof.get('receiver_epoch')!=window_context['receiver_epoch']
                            or type(proof.get('observed_at')) not in (int,float)
                            or not window_started<=proof['observed_at']<=now):continue
                elif kind!='append':continue
                candidates.append(deepcopy(event))
            if not candidates:return {'state':'no_fresh_load'}
            # A batch can contain rapid playlist changes. Only the actual current
            # receiver clip can select its matching command; list order is not time.
            deadline=now+4
            while True:
                if self.context()!=window_context:return {'state':'context_changed'}
                actual=self.status()
                matching=[e for e in candidates if matches(actual,e['video_sha256'])]
                if matching:break
                if self.clock()>=deadline:return {'state':'incoming_unverified'}
                self.sleep(.2)
            # Two different Lounge sessions matching the same clip are ambiguous.
            if len({e['session_digest'] for e in matching})!=1:
                return {'state':'ambiguous_session'}
            selected=max(matching,key=lambda e:e['sequence'])
            reservation=self.ledger.reserve(window_context['receiver_epoch'],
                                            selected['session_digest'],selected['sequence'])
            if reservation!='reserved':return {'state':reservation}
            if self.context()!=window_context or not matches(self.status(),selected['video_sha256']):
                return {'state':'context_changed'}
            if window_context['mode']!='kiosk':
                # Adapter must use the registered incoming-preserving controller,
                # with this expected context and a finite timeout. Never ordinary
                # switch_mode/play/reload/restore-previous-YouTube paths.
                sent=True
                self.handoff(selected['video_sha256'],expected_context=deepcopy(window_context),timeout=40)
            observed=self.verify()
            if (not isinstance(observed,dict) or observed.get('endpoint')!='home_edge_01'
                    or observed.get('receiver_epoch')!=window_context['receiver_epoch']
                    or observed.get('mode')!='kiosk' or observed.get('foreground_chrome') is not True
                    or observed.get('mpv_overlay_hidden') is not True
                    or not matches(observed.get('player'),selected['video_sha256'])):
                return {'state':'handoff_unverified','sent':sent}
            # Publish YouTube directly. No intermediate Video, endpoint or volume write.
            self.publish({'mode':'kiosk','reason':'fresh_receiver_media_load',
                'endpoint':'home_edge_01','receiver_epoch':window_context['receiver_epoch'],
                'sequence':selected['sequence'],'session_digest':selected['session_digest'],
                'video_sha256':selected['video_sha256']})
            return {'state':'youtube_verified','sent':sent,'published':True}
        except Exception:
            return {'state':'failed','sent':sent}
        finally:self._lock.release()
