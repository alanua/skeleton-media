import ast
import os
from pathlib import Path
import types
import unittest


ROOT=Path(os.environ['PRIVATE_SOURCE_ROOT'])


class ObservationOnlyTests(unittest.TestCase):
    def test_existing_recovery_is_unchanged(self):
        def recover(path):
            tree=ast.parse(path.read_text());return next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='recover')
        self.assertEqual(ast.dump(recover(ROOT/'home-edge-youtube-stability'),include_attributes=False),
                         ast.dump(recover(ROOT.parent/'backend-baseline/home-edge-youtube-stability'),include_attributes=False))
    def test_status_exposes_progress_without_repair(self):
        tree=ast.parse((ROOT/'home-edge-youtube-stability').read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='status')
        calls=[]
        scope={'http':lambda *args,**kwargs:{'running':True,'pause':False},
            'subprocess':types.SimpleNamespace(run=lambda *args,**kwargs:types.SimpleNamespace(stdout='MODE=kiosk\n')),
            '_mode_name':lambda x:'kiosk','youtube_health':lambda:({'state':'stalled','healthy':False}),
            'recover':lambda:calls.append('unexpected recovery')}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'<isolated status>','exec'),scope)
        result=scope['status']();self.assertEqual(result['youtube_health'],{'state':'stalled','healthy':False});self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
