import ast
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

from bounded_hls_resolver import BoundedResolver, ResolveUnavailable, prepare_recovery_source
from patch_refresh_resolver import patch
import test_source_refresh_preservation as preservation

ROOT = Path(__file__).resolve().parent


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.worker = self.root/'worker.py'

    def worker_code(self, code):
        self.worker.write_text(code)
        return BoundedResolver(self.worker)

    def test_actual_child_receives_origin_and_returns_real_resolver_shape(self):
        r = self.worker_code("import sys,json\nv=json.load(sys.stdin)\nprint(json.dumps({'ok':True,'result':{'title':'Title','poster':'','sources':[{'url':v['page_url']}],'errors':[]}}))")
        result = r('https://example.invalid/owned')
        self.assertEqual(result['sources'][0]['url'], 'https://example.invalid/owned')
        self.assertNotIn('tmdb_id', result)

    def test_timeout_terminates_real_child_process_group(self):
        pidfile = self.root/'child-pid'
        code = ("import os,subprocess,sys,time\n"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])\n"
                f"open({str(pidfile)!r},'w').write(str(p.pid))\n"
                "time.sleep(20)\n")
        r = self.worker_code(code)
        started = time.monotonic()
        with self.assertRaisesRegex(ResolveUnavailable,'deadline'):
            r('https://example.invalid/owned', timeout=.4)
        self.assertLess(time.monotonic()-started, 2)
        pid = int(pidfile.read_text())
        # A killed orphan may briefly remain a zombie awaiting init's reaper.
        status = Path(f'/proc/{pid}/stat')
        try:
            state=status.read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError):
            state='reaped'
        self.assertIn(state, ('Z','reaped'))

    def test_error_protocol_never_exposes_raw_url_or_exception(self):
        for code in [
            "raise RuntimeError('PRIVATE_SIGNED_STREAM')",
            "print('PRIVATE_SIGNED_STREAM')",
            "print('{}')",
            "print('{\"ok\":true,\"result\":{}}')",
        ]:
            r=self.worker_code(code)
            with self.assertRaises(ResolveUnavailable) as error:
                r('https://example.invalid/owned')
            self.assertNotIn('PRIVATE',str(error.exception))

    def test_timeout_bounds_and_invalid_origin_never_start_child(self):
        r=self.worker_code("raise AssertionError('must not run')")
        for timeout in [0,-1,13,float('nan'),True]:
            with self.assertRaises(ValueError):
                r('https://example.invalid',timeout=timeout)
        with self.assertRaises(ResolveUnavailable):
            r('file:///private')

    def test_worker_uses_sibling_resolver_without_importing_application(self):
        self.worker.write_text((ROOT/'hls_resolver_request.py').read_text())
        (self.root/'resolver.py').write_text(
            "def resolve_page(origin):\n return {'title':'T','poster':'','sources':[{'url':origin}],'errors':[]}\n")
        (self.root/'app.py').write_text("raise AssertionError('app must not import')")
        result=BoundedResolver(self.worker)('https://example.invalid/owned')
        self.assertEqual(result['title'],'T')

    def test_worker_failure_is_sanitized(self):
        self.worker.write_text((ROOT/'hls_resolver_request.py').read_text())
        (self.root/'resolver.py').write_text(
            "def resolve_page(origin):\n raise RuntimeError('PRIVATE_SIGNED_STREAM')\n")
        with self.assertRaises(ResolveUnavailable) as error:
            BoundedResolver(self.worker)('https://example.invalid/owned')
        self.assertNotIn('PRIVATE',str(error.exception))


class ActualSelectorInjection(unittest.TestCase):
    def setUp(self):
        self.fixture=preservation.RefreshPreservation()
        self.fixture.setUp()
        source=(ROOT/'work/backend/app.py').read_text()
        fn=next(n for n in ast.parse(source).body
                if isinstance(n,ast.FunctionDef) and n.name=='_refresh_stale_source')
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<actual-patched-selector>','exec'),self.fixture.ns)
        self.refresh=self.fixture.ns['_refresh_stale_source']

    def test_current_selector_uses_bounded_injection_without_persisting(self):
        f=self.fixture
        calls=[]
        def resolver(origin,timeout):
            calls.append((origin,timeout));return f.result
        fresh=prepare_recovery_source(f.job,f.stale,refresh=self.refresh,resolver=resolver)
        self.assertEqual(fresh['group'],'Українська')
        self.assertEqual(calls,[(f.job['page_url'],12)])
        self.assertEqual(f.calls,[])
        self.assertEqual(f.saved,[])

    def test_uninjected_normal_retry_is_unchanged(self):
        f=self.fixture
        self.refresh(f.job,f.stale)
        self.assertEqual(len(f.saved),1)
        self.assertEqual(f.calls,[f.job['page_url']])

    def test_injected_timeout_does_not_change_job_or_save(self):
        f=self.fixture;before=copy.deepcopy(f.job)
        def fail(*a,**kw):raise ResolveUnavailable('deadline')
        with self.assertRaises(ResolveUnavailable):
            prepare_recovery_source(f.job,f.stale,refresh=self.refresh,resolver=fail)
        self.assertEqual(f.job,before);self.assertEqual(f.saved,[])

    def test_actual_selector_still_rejects_wrong_voice_and_season(self):
        f=self.fixture
        for field,value in [('group','Other'),('season','9')]:
            original=copy.deepcopy(f.result);before=copy.deepcopy(f.job)
            f.result['sources'][0][field]=value
            with self.assertRaises(RuntimeError):
                prepare_recovery_source(f.job,f.stale,refresh=self.refresh,
                    resolver=lambda *a,**kw:f.result)
            self.assertEqual(f.job,before);self.assertEqual(f.saved,[])
            f.result=original

    def test_exact_patch_idempotency_and_drift_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'app.py';p.write_text((ROOT/'app.before.py').read_text())
            self.assertTrue(patch(p));self.assertFalse(patch(p))
            p.write_text(p.read_text()+'\n# source drift\n')
            with self.assertRaises(ValueError):patch(p)


if __name__ == '__main__':
    unittest.main()
