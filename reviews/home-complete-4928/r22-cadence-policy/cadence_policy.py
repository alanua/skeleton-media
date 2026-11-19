"""Bounded opt-in cadence transaction using explicit canonical operation adapters.

No executor, timer, display command or automatic activation is created on import.
Action admission must come from the existing approved Home Edge operation context.
"""
from copy import deepcopy
import hashlib
import json
import math
import re
import threading

IDENTITY=('endpoint','mode','session','process','revision','job_id','source_id','path_sha256','policy_generation')
DIGEST=re.compile(r'^[a-f0-9]{64}$')


def number(value):
    if isinstance(value,bool):return None
    try:value=float(value)
    except (ValueError,TypeError,OverflowError):return None
    return value if math.isfinite(value) else None


def constant_pts_rate(values):
    """Use actual observed video PTS intervals, never time * estimated FPS."""
    if not isinstance(values,list) or not 20<=len(values)<=240:return None
    pts=[number(v) for v in values]
    if any(v is None for v in pts):return None
    delta=[b-a for a,b in zip(pts,pts[1:])]
    if not delta or any(d<=0 for d in delta):return None
    for fps in (25,30,60):
        if all(abs(d-1/fps)<=.0003 for d in delta):return fps
    return None


def eligible(snapshot):
    return (isinstance(snapshot,dict) and snapshot.get('endpoint')=='home_edge_01'
        and snapshot.get('mode')=='mpv' and snapshot.get('live') is False
        and snapshot.get('pause') is False and snapshot.get('seeking') is False
        and snapshot.get('paused_for_cache') is False and snapshot.get('transition') is False
        and snapshot.get('operator_override') is False and snapshot.get('loaded') is True
        and isinstance(snapshot.get('session'),str) and DIGEST.fullmatch(snapshot['session'])
        and isinstance(snapshot.get('process'),tuple) and len(snapshot['process'])==3
        and bool(snapshot['process'][0]) and type(snapshot['process'][1]) is int and snapshot['process'][1]>0
        and str(snapshot['process'][2]).isdigit()
        and type(snapshot.get('revision')) is int and snapshot['revision']>=0
        and number(snapshot.get('position')) is not None)


def same(first,current):
    return eligible(current) and all(first.get(k)==current.get(k) for k in IDENTITY)


def display_valid(value):
    return (isinstance(value,dict) and value.get('logical_count')==1
        and value.get('physical_count')==1 and isinstance(value.get('identity'),str)
        and DIGEST.fullmatch(value['identity']) and type(value.get('width')) is int
        and type(value.get('height')) is int and value['width']>0 and value['height']>0
        and isinstance(value.get('mode_id'),str) and bool(value['mode_id'])
        and number(value.get('hz')) is not None and isinstance(value.get('supported'),dict)
        and isinstance(value.get('layout_digest'),str) and DIGEST.fullmatch(value['layout_digest']))


class CadencePolicy:
    def __init__(self):
        self.lock=threading.Lock()

    def run(self,*,config,snapshot,display,sample_pts,read_state,write_state,
            admit,execute,mutation_guard,clock,sleep,audit):
        if not self.lock.acquire(blocking=False):return {'state':'busy','applied':False}
        attempted=False;actual_applied=False
        try:
            if (not isinstance(config,dict) or config.get('enabled') is not True
                    or config.get('schema')!='skeleton.cadence.opt_in.v1'):
                return {'state':'disabled','applied':False}
            receipt=config.get('approval_receipt')
            if (config.get('endpoint')!='home_edge_01'
                    or not isinstance(receipt,str) or not DIGEST.fullmatch(receipt)
                    or not isinstance(config.get('generation'),str) or not DIGEST.fullmatch(config['generation'])
                    or not config.get('operator_approval_ref')
                    or not isinstance(config.get('display_identity'),str)
                    or not DIGEST.fullmatch(config['display_identity'])):
                return {'state':'approval_missing','applied':False}
            first=snapshot()
            if not eligible(first) or first.get('policy_generation')!=config['generation']:return {'state':'ineligible','applied':False}
            before=display()
            if not display_valid(before) or before['identity']!=config['display_identity']:
                return {'state':'display_mismatch','applied':False}
            sampled_at=clock();pts=sample_pts(timeout=3)
            rate=constant_pts_rate(pts)
            after_sample=snapshot()
            elapsed=clock()-sampled_at
            if (not same(first,after_sample) or not .5<=elapsed<=4
                    or not .3<=after_sample['position']-first['position']<=6):
                return {'state':'sample_unverified','applied':False}
            if rate is None:return {'state':'cadence_unreliable','applied':False}
            target=50 if rate==25 else 60
            if abs(before['hz']-target)<=.2:
                return {'state':'already_matched','applied':False,'fps':rate}
            target_mode=before['supported'].get(str(target))
            if not isinstance(target_mode,str) or not target_mode:
                return {'state':'unsupported','applied':False}
            state=read_state()
            if (not isinstance(state,dict)
                    or ('attempts' in state and (type(state['attempts']) is not int or not 0<=state['attempts']<=2))
                    or ('last_attempt' in state and number(state['last_attempt']) is None)):
                return {'state':'state_unavailable','applied':False}
            now=clock()
            previous=number(state.get('last_attempt'))
            if previous is not None and state.get('boot')==first['process'][0] and now-previous<60:
                return {'state':'cooldown','applied':False}
            if state.get('session')==first['session'] and state.get('attempts',0)>=2:
                return {'state':'retry_exhausted','applied':False}
            if (state.get('session')==first['session'] and state.get('owned_mode_id')
                    and state['owned_mode_id']!=before['mode_id']):
                return {'state':'operator_override','applied':False}
            operation='media_display_refresh_'+str(target)+'_v1'
            key=hashlib.sha256(json.dumps([first['session'],first['revision'],
                first['source_id'],before['mode_id'],target,receipt],separators=(',',':')).encode()).hexdigest()
            request=dict(operation_id=operation,node_id='home-edge-01',run_as='desktop-user',
                execution_lane='routine_mutation',timeout_seconds=15,request_id='cadence-'+key,
                idempotency_key='cadence-'+key,operator_approval_ref=config['operator_approval_ref'],
                approval_receipt=receipt)
            decision=admit(deepcopy(request))
            if (not isinstance(decision,dict) or decision.get('allowed') is not True
                    or decision.get('approval_receipt')!=receipt
                    or decision.get('operation_id')!=operation):
                return {'state':'action_gate_blocked','applied':False}
            with mutation_guard():
                check=snapshot();observed=display()
                if (not same(first,check) or not display_valid(observed)
                        or observed!=before):
                    return {'state':'context_changed','applied':False}
                attempts=(state.get('attempts',0) if state.get('session')==first['session'] else 0)+1
                reserved=dict(state,session=first['session'],boot=first['process'][0],attempts=attempts,last_attempt=now)
                write_state(deepcopy(reserved))
                if read_state()!=reserved:return {'state':'reservation_unverified','applied':False}
                # The supplied executor is the registered approved operation,
                # never an arbitrary shell/display property from media metadata.
                audit(dict(request=request,state='reserved_before_execution',before=before,session=first['session']))
                attempted=True;actual_applied=None;command_error=False
                try:execute(deepcopy(request))
                except Exception:command_error=True
            sleep(1)
            actual=display();end=snapshot()
            actual_applied=(display_valid(actual) and actual['identity']==before['identity']
                and actual['mode_id']==target_mode and abs(actual['hz']-target)<=.2)
            verified=(display_valid(actual) and same(first,end)
                and actual['identity']==before['identity']
                and actual['layout_digest']==before['layout_digest']
                and (actual['width'],actual['height'])==(before['width'],before['height'])
                and actual['mode_id']==target_mode and abs(actual['hz']-target)<=.2
                and .3<=end['position']-after_sample['position']<=8)
            if verified:
                final=dict(reserved,owned_mode_id=actual['mode_id'],previous_hz=before['hz'])
                write_state(final)
                if read_state()!=final:verified=False
            if verified:
                audit(dict(request=request,state='applied_and_progress_verified',
                           before=before,after=actual,session=first['session'],command_error=command_error))
                return {'state':'applied_and_progress_verified','applied':True,'progress_verified':True,'fps':rate,'hz':target}
            # Roll back only if this exact trial still owns display AND playback.
            # Never overwrite a newer mode/session/operator selection.
            rollback=False
            if (display_valid(actual) and same(first,end) and actual['identity']==before['identity']
                    and actual['mode_id']==target_mode and actual['layout_digest']==before['layout_digest']):
                old=50 if abs(before['hz']-50)<=.2 else 60 if abs(before['hz']-60)<=.2 else None
                if old is not None:
                    reverse=dict(request,operation_id='media_display_refresh_'+str(old)+'_v1',
                        request_id=request['request_id']+'-rollback',
                        idempotency_key=request['idempotency_key']+'-rollback')
                    gate=admit(deepcopy(reverse))
                    if (isinstance(gate,dict) and gate.get('allowed') is True
                            and gate.get('approval_receipt')==receipt
                            and gate.get('operation_id')==reverse['operation_id']):
                        with mutation_guard():
                            if same(first,snapshot()) and display()==actual:
                                execute(reverse)
                                restored=display()
                                rollback=(display_valid(restored) and restored==before and same(first,snapshot()))
                                if rollback:actual_applied=False
            audit(dict(request=request,state='verification_failed',rollback_verified=rollback,
                       before=before,after=actual,session=first['session']))
            return {'state':'verification_failed','applied':actual_applied,'progress_verified':False,'rollback_verified':rollback}
        except Exception:
            return {'state':'failed','applied':actual_applied,'attempted':attempted}
        finally:self.lock.release()
