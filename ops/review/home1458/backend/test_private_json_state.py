import concurrent.futures
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock
import private_json_state as module

class PrivateJsonTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.path=self.root/'last-vod.json'
    def tearDown(self):self.directory.cleanup()
    def write(self,value):return module.atomic_private_json(self.path,value)
    def test_complete_nested_identity_and_progress_round_trip(self):
        value={'job_id':'fixture-job','source_id':'fixture-source','media_uid':'fixture-work','season':2,'episode':3,'time_pos':42.5,'title':'Тест'}
        self.write(value);self.assertEqual(json.loads(self.path.read_text()),value)
    def test_permissions_private_even_when_replacing_public_mode(self):
        self.path.write_text('{}');self.path.chmod(0o644)
        self.write({'value':1});self.assertEqual(stat.S_IMODE(self.path.stat().st_mode),0o600)
    def test_parent_creation(self):
        self.path=self.root/'nested'/'last.json';self.write({'x':1});self.assertEqual(json.loads(self.path.read_text()),{'x':1})
    def test_failed_serialization_preserves_old_file(self):
        self.write({'previous':True});before=self.path.read_bytes()
        with self.assertRaises(TypeError):self.write({'bad':object()})
        self.assertEqual(self.path.read_bytes(),before)
    def test_failed_replace_preserves_last_good_and_cleans_temporary(self):
        self.write({'previous':True});before=self.path.read_bytes()
        with mock.patch.object(module.os,'replace',side_effect=OSError('fixture replace failure')):
            with self.assertRaises(OSError):self.write({'new':True})
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual([x for x in self.root.iterdir() if x!=self.path],[])
    def test_concurrent_writers_have_independent_temporary_paths(self):
        replace=os.replace;barrier=threading.Barrier(2);seen=[];guard=threading.Lock()
        def simultaneous(source,target):
            with guard:seen.append(str(source))
            barrier.wait(timeout=3)
            return replace(source,target)
        with mock.patch.object(module.os,'replace',side_effect=simultaneous):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results=[pool.submit(self.write,{'writer':n,'payload':'x'*5000}) for n in range(2)]
                for result in results:result.result(timeout=5)
        self.assertEqual(len(set(seen)),2)
        self.assertIn(json.loads(self.path.read_text())['writer'],[0,1])
    def test_repeated_updates_leave_valid_last_record(self):
        for n in range(10):self.write({'revision':n})
        self.assertEqual(json.loads(self.path.read_text()),{'revision':9})
        self.assertEqual(list(self.root.iterdir()),[self.path])
    def test_file_and_directory_are_synced(self):
        sync=os.fsync;calls=[]
        def record(fd):
            calls.append(stat.S_ISDIR(os.fstat(fd).st_mode));return sync(fd)
        with mock.patch.object(module.os,'fsync',side_effect=record):self.write({'x':1})
        self.assertIn(False,calls);self.assertIn(True,calls)
    def test_sync_failure_before_replace_preserves_last_good(self):
        self.write({'previous':True});before=self.path.read_bytes()
        with mock.patch.object(module.os,'fsync',side_effect=OSError('fixture sync failure')):
            with self.assertRaises(OSError):self.write({'new':True})
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(list(self.root.iterdir()),[self.path])
if __name__=='__main__':unittest.main()
