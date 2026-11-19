import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from youtube_incoming_runtime import owned_chrome_process

class ProcessIdentityTests(unittest.TestCase):
    def fixture(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        root=Path(temp.name);proc=root/'42';proc.mkdir()
        (proc/'comm').write_text('chrome\n')
        (proc/'stat').write_text('42 (chrome) '+' '.join(['S']+['0']*18+['123']))
        boot=root/'sys/kernel/random';boot.mkdir(parents=True);(boot/'boot_id').write_text('fixture-boot')
        return root,proc
    def test_canonical_comm_owner_boot_and_start_are_bound_without_exe_readlink(self):
        root,proc=self.fixture();(proc/'exe').symlink_to('/unreadable-nonexistent-executable')
        self.assertEqual(owned_chrome_process(42,root),('fixture-boot',42,'123'))
    def test_wrong_process_name_is_refused(self):
        root,proc=self.fixture();(proc/'comm').write_text('other\n')
        with self.assertRaises(RuntimeError):owned_chrome_process(42,root)
    def test_wrong_owner_is_refused(self):
        root,proc=self.fixture();actual=os.getuid()
        with patch('youtube_incoming_runtime.os.getuid',return_value=actual+1):
            with self.assertRaises(RuntimeError):owned_chrome_process(42,root)
    def test_start_tick_change_is_a_new_identity(self):
        root,proc=self.fixture();before=owned_chrome_process(42,root)
        (proc/'stat').write_text('42 (chrome) '+' '.join(['S']+['0']*18+['124']))
        self.assertNotEqual(owned_chrome_process(42,root),before)
    def test_invalid_pid_start_or_boot_cannot_supply_identity(self):
        root,proc=self.fixture()
        for pid in (True,-1,0,'42'):
            with self.assertRaises(RuntimeError):owned_chrome_process(pid,root)
        (proc/'stat').write_text('invalid')
        with self.assertRaises((RuntimeError,IndexError)):owned_chrome_process(42,root)

if __name__=='__main__':unittest.main()
