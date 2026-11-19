"""Actual current selector -> transaction integration, synthetic transport only."""
import ast
import copy
from pathlib import Path
import unittest
import test_source_refresh_preservation as preservation
import test_hls_recovery_transaction as transaction
from bounded_hls_resolver import prepare_recovery_source

ROOT=Path(__file__).resolve().parent


class SelectorTransaction(unittest.TestCase):
    def fixture(self):
        selected=preservation.RefreshPreservation()
        selected.setUp()
        source=(ROOT/'work/backend/app.py').read_text()
        fn=next(n for n in ast.parse(source).body
                if isinstance(n,ast.FunctionDef) and n.name=='_refresh_stale_source')
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<current-selector>','exec'),selected.ns)
        f=transaction.Fixture()
        f.job=copy.deepcopy(selected.job)  # Real source uses job_id, not id.
        f.source=copy.deepcopy(selected.stale)
        f.source['kind']='hls'
        f.state.update(job_id='owned',source_id='old',season='2',episode='3',
                       voice='Українська',path=f.source['url'])
        def resolve(job,source,timeout):
            f.calls+=1
            return prepare_recovery_source(job,source,
                refresh=selected.ns['_refresh_stale_source'],
                resolver=lambda *a,**kw:copy.deepcopy(selected.result),timeout=timeout)
        f.resolve=resolve
        return f,selected

    def test_actual_job_id_and_real_resolver_shape_complete_transaction(self):
        f,selected=self.fixture()
        result=f.run()
        self.assertEqual(result['state'],'recovered')
        self.assertEqual(f.calls,1)
        self.assertEqual([c[0] for c in f.commands],['loadfile','set_property'])
        self.assertEqual(selected.saved,[])
        self.assertEqual(f.job['sources'][0]['url'],selected.stale['url'])
        committed_job,committed_source=f.commits[0]
        self.assertEqual(committed_job['job_id'],'owned')
        self.assertEqual(committed_source['group'],'Українська')
        self.assertEqual(committed_source['episode'],'3')
        self.assertEqual(committed_source['season'],'2')
        self.assertNotEqual(committed_source['url'],f.source['url'])

    def test_job_id_is_authoritative_over_unrelated_id_field(self):
        f,_=self.fixture();f.job['id']='unrelated-catalog-id'
        self.assertEqual(f.run()['state'],'recovered')

    def test_wrong_voice_from_actual_selector_never_loads(self):
        f,selected=self.fixture()
        selected.result['sources'][0]['group']='Other'
        self.assertEqual(f.run()['state'],'failed')
        self.assertEqual(f.commands,[])
        self.assertEqual(f.commits,[])
        self.assertEqual(selected.saved,[])

    def test_operator_pause_during_actual_preparation_prevents_load(self):
        f,selected=self.fixture();resolve=f.resolve
        def pause(*a,**kw):
            result=resolve(*a,**kw);f.rev+=1;f.state['pause']=True;return result
        f.resolve=pause
        self.assertEqual(f.run()['state'],'cancelled')
        self.assertEqual(f.commands,[])
        self.assertEqual(f.commits,[])
        self.assertEqual(selected.saved,[])


if __name__=='__main__':
    unittest.main()
