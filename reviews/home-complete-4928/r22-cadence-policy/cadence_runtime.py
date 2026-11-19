"""Serial, default-off cadence adapter; requires a canonical gateway object.

The product integration must supply the EXISTING approved gateway. There is no
subprocess/display fallback and no access to executor credentials. Merely wiring
this adapter cannot enable policy or authorize an operation.
"""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import threading
import time

from cadence_policy import CadencePolicy, eligible, same
from cadence_state import CadenceStore, read_opt_in, atomic_private
from bounded_packet_pts_probe import BoundedPacketPTSProbe
from cadence_display import read_display

class CadenceRuntime:
    def __init__(self,*,config_path,root,snapshot,source,mutation_guard,gateway=None,
                 display=read_display,probe=None,clock=time.monotonic,sleep=time.sleep):
        self.config_path=Path(config_path);self.root=Path(root)
        self.snapshot_provider=snapshot;self.source_provider=source
        self.mutation_guard=mutation_guard;self.gateway=gateway
        self.display=display;self.probe=probe or BoundedPacketPTSProbe()
        self.clock=clock;self.sleep=sleep;self.lock=threading.Lock()
        self.engine=CadencePolicy();self.settled=None;self.settled_at=None

    def tick(self):
        if not self.lock.acquire(blocking=False):return {'state':'busy','applied':False}
        store=None
        try:
            config=read_opt_in(self.config_path)
            if config.get('enabled') is not True:
                self.settled=None
                return {'state':'disabled','applied':False}
            # Do this BEFORE even probing a provider. An unbound deployment
            # cannot incur network work or invoke a direct display helper.
            if self.gateway is None:return {'state':'action_gate_unavailable','applied':False}
            def snapshot():
                value=deepcopy(self.snapshot_provider())
                # Fresh config generation is fenced on EVERY observation.
                latest=read_opt_in(self.config_path)
                value['policy_generation']=latest.get('generation') if latest.get('enabled') is True else None
                return value
            first=snapshot()
            if not eligible(first):self.settled=None;return {'state':'ineligible','applied':False}
            now=self.clock()
            identity=tuple(str(first.get(k)) for k in ('session','process','revision','source_id','path_sha256','policy_generation'))
            if self.settled!=identity:
                self.settled=identity;self.settled_at=now
                return {'state':'settling','applied':False}
            if now-self.settled_at<10:return {'state':'settling','applied':False}
            # Caller must supply the independently observed MPV cache headroom.
            if type(first.get('buffer_seconds')) not in (int,float) or not 30<=first['buffer_seconds']<=86400:
                return {'state':'buffer_headroom_unverified','applied':False}
            self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
            if self.root.is_symlink() or self.root.stat().st_uid!=os.getuid() or self.root.stat().st_mode&0o077:
                raise ValueError('owned private cadence root required')
            store=CadenceStore(self.root/'budget')
            def sample_pts(timeout):
                before=snapshot()
                if not same(first,before) or before.get('buffer_seconds',0)<30:return None
                selected=self.source_provider()
                if (not isinstance(selected,dict) or selected.get('kind')!='hls'
                        or selected.get('live') is not False or selected.get('drm') is not False):
                    return None
                url=selected.get('url')
                if not isinstance(url,str) or hashlib.sha256(url.encode()).hexdigest()!=before['path_sha256']:
                    return None
                if not store.reserve_probe(session=first['session'],boot=first['process'][0],now=self.clock()):
                    return None
                started=self.clock()
                points=self.probe(url,before['position'],headers=selected.get('headers',{}),timeout=timeout)
                # A fast local/remote response still needs independent live
                # position advancement over a real observation interval.
                elapsed=self.clock()-started
                if elapsed<.6:self.sleep(.6-elapsed)
                return points if same(first,snapshot()) else None
            allowed={'media_display_refresh_50_v1','media_display_refresh_60_v1'}
            def admit(request):
                if request.get('operation_id') not in allowed:return {'allowed':False}
                if read_opt_in(self.config_path)!=config:return {'allowed':False}
                return self.gateway.admit(deepcopy(request))
            def execute(request):
                if request.get('operation_id') not in allowed or read_opt_in(self.config_path)!=config:
                    raise ValueError('cadence approval changed')
                # Gateway owns exact registered argv, auditing, run_as, lane,
                # deadline and idempotency. No locally rebuilt shell command.
                return self.gateway.execute(deepcopy(request))
            def audit(value):
                atomic_private(self.root/'last-audit.json',value)
            return self.engine.run(config=config,snapshot=snapshot,display=self.display,
                sample_pts=sample_pts,read_state=lambda:store.read(first['session']),
                write_state=store.write,admit=admit,execute=execute,
                mutation_guard=self.mutation_guard,clock=self.clock,sleep=self.sleep,audit=audit)
        except Exception:return {'state':'adapter_unavailable','applied':False}
        finally:
            if store is not None:store.close()
            self.lock.release()
