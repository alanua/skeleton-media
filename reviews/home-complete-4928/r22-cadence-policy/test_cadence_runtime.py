"""Real private persistence and serial adapter tests; display/gateway are synthetic."""
import copy
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from cadence_runtime import CadenceRuntime
from cadence_state import CadenceStore,atomic_private,read_opt_in
from test_cadence_policy import Fixture

class PersistentCadenceTests(unittest.TestCase):
    def root(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup)
        return Path(t.name)

    def test_absent_settings_are_disabled_and_do_not_create_a_file(self):
        p=self.root()/'absent.json'
        self.assertFalse(read_opt_in(p)['enabled']);self.assertFalse(p.exists())

    def test_opt_in_survives_reconstruction_with_private_atomic_permissions(self):
        p=self.root()/'config/settings.json';value=Fixture().config
        atomic_private(p,value)
        self.assertEqual(read_opt_in(p),value);self.assertEqual(p.stat().st_mode&0o777,0o600)
        atomic_private(p,dict(value,enabled=False))
        self.assertFalse(read_opt_in(p)['enabled'])
        self.assertEqual(list(p.parent.glob('.*')),[])

    def test_config_symlink_public_permissions_and_malformed_json_fail_closed(self):
        root=self.root();p=root/'settings';atomic_private(p,Fixture().config)
        link=root/'link';link.symlink_to(p)
        with self.assertRaises(OSError):read_opt_in(link)
        p.chmod(0o644)
        with self.assertRaises(ValueError):read_opt_in(p)
        p.chmod(0o600);p.write_text('{')
        with self.assertRaises(ValueError):read_opt_in(p)

    def test_retry_cap_survives_reconstruction_and_intervening_session(self):
        root=self.root();s=CadenceStore(root/'budget')
        for session,n in [('a'*64,1),('a'*64,2),('b'*64,1)]:
            s.write(dict(session=session,attempts=n,boot='boot',last_attempt=100+n))
        s.close();s=CadenceStore(root/'budget');self.addCleanup(s.close)
        self.assertEqual(s.read('a'*64)['attempts'],2)
        self.assertFalse(s.reserve_probe(session='a'*64,boot='boot',now=300))
        with self.assertRaises(ValueError):s.write(dict(session='a'*64,attempts=1,boot='boot',last_attempt=300))

    def test_probe_failure_is_rate_limited_across_sessions_and_restart(self):
        root=self.root();s=CadenceStore(root/'budget')
        self.assertTrue(s.reserve_probe(session='a'*64,boot='boot',now=100));s.close()
        s=CadenceStore(root/'budget');self.addCleanup(s.close)
        self.assertFalse(s.reserve_probe(session='b'*64,boot='boot',now=159))
        self.assertTrue(s.reserve_probe(session='b'*64,boot='boot',now=160))
        self.assertFalse(s.reserve_probe(session='c'*64,boot='boot',now=90))
        self.assertTrue(s.reserve_probe(session='c'*64,boot='newboot',now=2))

    def test_capacity_is_fail_closed_without_evicting_a_retry_bound(self):
        s=CadenceStore(self.root()/'budget',capacity=1);self.addCleanup(s.close)
        row=dict(session='a'*64,attempts=2,boot='boot',last_attempt=100)
        s.write(row)
        with self.assertRaises(ValueError):s.write(dict(row,session='b'*64,attempts=1))
        self.assertFalse(s.reserve_probe(session='b'*64,boot='boot',now=200))
        self.assertEqual(s.read('a'*64)['attempts'],2)

    def fixture(self,gateway=True):
        root=self.root();f=Fixture();p=root/'settings.json';atomic_private(p,f.config)
        url='https://example.invalid/private-selected.m3u8'
        f.context['path_sha256']=hashlib.sha256(url.encode()).hexdigest()
        f.context['buffer_seconds']=35
        f.source=dict(kind='hls',live=False,drm=False,url=url)
        f.probes=0;f.before_probe=lambda:None
        def probe(url,position,headers,timeout):
            f.probes+=1;f.before_probe()
            return f.sample(timeout)
        class Gateway:
            def admit(self,request):return f.admit(request)
            def execute(self,request):return f.execute(request)
        r=CadenceRuntime(config_path=p,root=root/'cadence',
            snapshot=f.snapshot,source=lambda:copy.deepcopy(f.source),
            mutation_guard=nullcontext,gateway=Gateway() if gateway else None,
            display=lambda:copy.deepcopy(f.monitor),probe=probe,
            clock=lambda:f.now,sleep=f.sleep)
        return f,r,p,root

    def settle(self,f,r):
        self.assertEqual(r.tick()['state'],'settling')
        f.now+=11;f.position+=11

    def test_disabled_and_missing_gateway_never_probe_or_actuate(self):
        f,r,p,root=self.fixture(False)
        self.assertEqual(r.tick()['state'],'action_gate_unavailable')
        atomic_private(p,dict(f.config,enabled=False))
        self.assertEqual(r.tick()['state'],'disabled')
        self.assertEqual(f.probes,0);self.assertEqual(f.actions,[])
        self.assertFalse((root/'cadence').exists())

    def test_serial_enabled_adapter_uses_gateway_and_verifies_live_progress(self):
        f,r,p,root=self.fixture();self.settle(f,r)
        result=r.tick();self.assertEqual(result['state'],'applied_and_progress_verified',result)
        self.assertEqual(len(f.actions),1);self.assertEqual(f.probes,1)
        audit=json.loads((root/'cadence/last-audit.json').read_text())
        self.assertEqual(audit['state'],'applied_and_progress_verified')
        self.assertNotIn(f.source['url'],json.dumps(audit))

    def test_low_buffer_pause_seek_and_other_mode_do_not_probe(self):
        for key,value in [('buffer_seconds',29),('pause',True),('seeking',True),('mode','kiosk')]:
            f,r,p,root=self.fixture();self.settle(f,r);f.context[key]=value
            self.assertFalse(r.tick()['applied']);self.assertEqual(f.probes,0);self.assertFalse(f.actions)

    def test_config_disabled_during_probe_blocks_all_actions(self):
        f,r,p,root=self.fixture();self.settle(f,r)
        f.before_probe=lambda:atomic_private(p,dict(f.config,enabled=False))
        self.assertFalse(r.tick()['applied']);self.assertFalse(f.actions)

    def test_source_change_during_probe_blocks_all_actions(self):
        f,r,p,root=self.fixture();self.settle(f,r)
        f.before_probe=lambda:f.context.update(source_id='new')
        self.assertFalse(r.tick()['applied']);self.assertFalse(f.actions)

    def test_exact_selected_url_is_required_before_probe(self):
        f,r,p,root=self.fixture();self.settle(f,r)
        f.source['url']+='?other=1'
        self.assertEqual(r.tick()['state'],'sample_unverified')
        self.assertEqual(f.probes,0);self.assertFalse(f.actions)

    def test_failed_and_already_matching_probes_are_throttled(self):
        for invalid in (True,False):
            f,r,p,root=self.fixture();self.settle(f,r)
            if invalid:f.points=[0.,1.,2.]
            else:f.monitor.update(hz=50.,mode_id='m50')
            self.assertFalse(r.tick()['applied'])
            f.now+=3;f.position+=3
            self.assertFalse(r.tick()['applied']);self.assertEqual(f.probes,1)
            self.assertFalse(f.actions)

    def test_transition_resets_settling_window(self):
        f,r,p,root=self.fixture();self.settle(f,r)
        f.context.update(transition=True)
        self.assertEqual(r.tick()['state'],'ineligible')
        f.context.update(transition=False)
        self.assertEqual(r.tick()['state'],'settling')
        self.assertEqual(f.probes,0)

    def test_overlapping_tick_cannot_create_parallel_probe(self):
        f,r,p,root=self.fixture();r.lock.acquire()
        try:self.assertEqual(r.tick()['state'],'busy')
        finally:r.lock.release()
        self.assertEqual(f.probes,0);self.assertFalse(f.actions)

if __name__=='__main__':unittest.main()
