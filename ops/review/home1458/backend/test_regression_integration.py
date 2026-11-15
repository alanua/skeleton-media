"""Exercise patched private functions with synthetic state and no live imports."""
import ast
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest

from boot_vod_capture import load_last_vod
from media_control_context import require_expected_mode, StaleMediaControl
from source_page_provenance import verified_group_source_sites, source_page_provenance

ROOT=Path(os.environ.get('PRIVATE_SOURCE_ROOT',Path(__file__).parent))


def load_function(file,name,scope):
    scope.setdefault('Any',object)
    tree=ast.parse((ROOT/file).read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    node.decorator_list=[]
    module=ast.Module(body=[node],type_ignores=[])
    exec(compile(module,str(ROOT/file),'exec'),scope)
    return scope[name]


class ContextTests(unittest.TestCase):
    def test_optional_backwards_compatibility(self):
        require_expected_mode({},'unknown')

    def test_aliases(self):
        for expected,actual in [('youtube','kiosk'),('video','mpv'),('game','games'),('tv','tv')]:
            require_expected_mode({'expected_mode':expected},actual)

    def test_stale_or_malformed_is_rejected(self):
        for expected in ['unknown','tv',None,False,{},' KIOSK ']:
            with self.subTest(expected=expected),self.assertRaises(StaleMediaControl):
                require_expected_mode({'expected_mode':expected},'kiosk')

    def test_routes_reject_before_dispatch(self):
        from flask import Flask,jsonify,request
        app=Flask(__name__)
        calls=[]
        scope={'Response':object,'request':request,'jsonify':jsonify,
               '_MODE_LOCK':threading.Lock(),'_require':lambda:None,
               '_media_endpoint_alias_required':lambda eid:'tv',
               '_media_endpoint_mode':lambda alias:{'mode':'games'},
               'require_expected_mode':require_expected_mode,'StaleMediaControl':StaleMediaControl,
               '_remote_control':lambda *args:calls.append(args),
               '_media_endpoint_seek':lambda *args:calls.append(args),
               'player':types.SimpleNamespace(control=lambda *args:calls.append(args))}
        load_function('app.py','_require_media_control_context',scope)
        from werkzeug.exceptions import Conflict
        for name,args in [('media_endpoint_remote_control_v1',('home_edge_tv',)),
                          ('media_endpoint_control_v1',('home_edge_tv','stop')),
                          ('media_endpoint_seek_v1',('home_edge_tv',))]:
            fn=load_function('app.py',name,scope)
            with app.test_request_context(json={'expected_mode':'kiosk','action':'up','position':5}):
                with self.assertRaises(Conflict):fn(*args)
        self.assertEqual(calls,[])

    def test_mode_and_actions_share_lock(self):
        tree=ast.parse((ROOT/'app.py').read_text())
        names={'mode_set','media_endpoint_mode_post_v1','media_endpoint_remote_control_v1',
               'media_endpoint_control_v1','media_endpoint_seek_v1'}
        for node in tree.body:
            if isinstance(node,ast.FunctionDef) and node.name in names:
                self.assertTrue(any(isinstance(n,ast.With) and any(isinstance(x.context_expr,ast.Name)
                    and x.context_expr.id=='_MODE_LOCK' for x in n.items) for n in ast.walk(node)),node.name)
                names.remove(node.name)
        self.assertEqual(names,set())


class BootTests(unittest.TestCase):
    def test_invalid_cache_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'last-vod.json'
            self.assertIsNone(load_last_vod(p))
            for content in ['', '{', 'null','[]','{}', '{"job_id":"a","source_id":"b","position_seconds":NaN}',
                            '{"job_id":"a","source_id":"b","position_seconds":-1}',
                            '{"job_id":"a","source_id":"b","position_seconds":true}']:
                p.write_text(content);self.assertIsNone(load_last_vod(p),content)
            p.write_bytes(b'\xff');self.assertIsNone(load_last_vod(p))

    def test_valid_cache_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'last-vod.json';value={'job_id':'a','source_id':'b','position_seconds':27.3,'paused':True}
            p.write_text(json.dumps(value));self.assertEqual(load_last_vod(p),value)


class BackendTests(unittest.TestCase):
    def test_explicit_mode_never_falls_back_to_warm_browser(self):
        for mode in ['mpv','tv','games','off']:
            def forbidden(*args,**kwargs):raise AssertionError('Warm browser fallback in explicit mode')
            scope={'_current_tv_mode':lambda:mode,'_youtube_web_media_status':forbidden,
                   '_browser_media_status':forbidden,'_player_active':forbidden}
            self.assertEqual(load_function('player.py','_active_backend',scope)(),(mode,None))

    def test_game_and_off_do_not_expose_parked_player_metadata(self):
        for mode in ['games','off']:
            scope={'Any':object,'_active_backend':lambda:(mode,None)}
            self.assertEqual(load_function('player.py','status',scope)(),{'running':False,'backend':mode})

    def test_history_projects_only_verified_source_pages(self):
        job={'job_id':'one','title':'Synthetic series','created_at':1,
             'catalog':{'media_type':'tv'},'sources':[
                 {'source_id':'a','source_page_url':'https://original.org/episode/1','season':'1'},
                 {'source_id':'b','source_page_url':'https://cdn.org/file.m3u8?token=secret'}]}
        scope={'verified_group_source_sites':verified_group_source_sites,
               '_history_catalog_job':lambda g:job,'_history_display_title':lambda j:j['title'],
               '_history_best_text':lambda rows:next((x[1] for x in rows if x[1]),''),
               '_history_source_season':lambda j,s:s.get('season',''),
               '_infer_content_type':lambda j,t:'series','_history_group_id':lambda key:'canonical',
               '_history_preferences_read':lambda:{},'_history_preference':lambda g:{},
               'media_discovery':types.SimpleNamespace(is_trailer_source=lambda s:False)}
        result=load_function('app.py','_history_item',scope)({'key':'one','jobs':[(2,job)]})
        self.assertEqual(result['source_sites'],[{'source_page_url':'https://original.org/episode/1'}])
        self.assertEqual(result['history_id'],'canonical')
        self.assertEqual(result['full_source_count'],2)
        self.assertNotIn('token',json.dumps(result))

    def test_current_metadata_binds_page_to_current_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);current=root/'current.json';current.write_text(json.dumps({'job_id':'one','source_id':'dub'}))
            (root/'one.json').write_text(json.dumps({'job_id':'one','sources':[
                {'source_id':'dub','source_page_url':'https://correct.org/episode/1'},
                {'source_id':'sub','source_page_url':'https://other.org/episode/1'}]}))
            scope={'Any':object,'json':json,'CURRENT':current,'JOBS':root,
                   'source_page_provenance':source_page_provenance,
                   '_canonical_display_title':lambda *args:'Synthetic',
                   '_canonical_season_poster':lambda *args:'','_versioned_poster':lambda v:v}
            result=load_function('player.py','_current_metadata',scope)()
            self.assertEqual(result['source_page_url'],'https://correct.org/episode/1')
            current.write_text(json.dumps({'job_id':'one','source_id':'missing',
                'source_page_url':'https://stale.org/episode/1','source_sites':[{'source_page_url':'https://stale.org'}]}))
            self.assertNotIn('source_page_url',scope['_current_metadata']())
            self.assertNotIn('source_sites',scope['_current_metadata']())


if __name__=='__main__':unittest.main()
