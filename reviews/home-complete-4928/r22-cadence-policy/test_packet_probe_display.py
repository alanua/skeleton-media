import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from bounded_packet_pts_probe import BoundedPacketPTSProbe,valid_url
from cadence_display import parse_mutter_state

class ProbeTests(unittest.TestCase):
    def fixture(self,data=None,code=0):
        self.calls=[];self.master=None;self.seals=None
        result=data if data is not None else dict(streams=[{'start_time':'1.4'}],
            packets=[{'pts_time':str(3.4+i*.04)} for i in range(30)])
        owner=self
        class Process:
            pid=999999
            def __init__(self,argv,**kwargs):
                owner.calls.append((argv,kwargs))
                fd=kwargs['pass_fds'][0]
                owner.master=os.pread(fd,65536,0).decode()
                owner.seals=fcntl.fcntl(fd,fcntl.F_GET_SEALS)
                os.write(kwargs['stdout'],json.dumps(result).encode())
                self.code=code
            def wait(self,timeout):return self.code
            def poll(self):return self.code
        return BoundedPacketPTSProbe(popen=Process)
    def test_actual_pts_values_are_sorted_without_fabrication_and_private_url_is_not_argv(self):
        probe=self.fixture()
        url='https://example.invalid/selected.m3u8?signature=dummy-secret'
        values=probe(url,2.)
        self.assertEqual(len(values),30)
        self.assertAlmostEqual(values[0],2.)
        argv=self.calls[0][0]
        self.assertNotIn(url,argv);self.assertNotIn('dummy-secret',' '.join(argv))
        self.assertIn(url,self.master)
        self.assertTrue(self.seals & fcntl.F_SEAL_WRITE)
        self.assertIn('--fsize=262144',argv)
    def test_untrusted_url_or_deadline_never_starts_child(self):
        probe=self.fixture()
        for url in ['file:///etc/passwd','https://user:password@example.invalid/x','https://example.invalid/x\nother','https://example.invalid/x#part']:
            self.assertIsNone(probe(url,0))
        for timeout in (True,0,4,float('nan')):
            self.assertIsNone(probe('https://example.invalid/x',0,timeout=timeout))
        self.assertEqual(self.calls,[])
    def test_credential_or_unknown_headers_never_improvise_auth_transport(self):
        probe=self.fixture()
        for headers in [{'Cookie':'private'},{'Authorization':'private'},
                        {'X-Private-Key':'private'},{'Referer':'ok\r\ninjected'}]:
            self.assertIsNone(probe('https://example.invalid/x',0,headers=headers))
        self.assertEqual(self.calls,[])
    def test_failed_incomplete_duplicate_or_wrong_time_packets_are_refused(self):
        variants=[{}, {'streams':[],'packets':[]},
            {'streams':[{'start_time':0}],'packets':[{'pts_time':0}]*30},
            {'streams':[{'start_time':0}],'packets':[{'pts_time':100+i*.04} for i in range(30)]}]
        for value in variants:
            probe=self.fixture(value)
            self.assertIsNone(probe('https://example.invalid/x',2))
        probe=self.fixture(code=1);self.assertIsNone(probe('https://example.invalid/x',2))
    def test_signed_input_descriptor_is_closed_after_probe(self):
        probe=self.fixture();probe('https://example.invalid/x',2)
        fd=self.calls[0][1]['pass_fds'][0]
        with self.assertRaises(OSError):os.fstat(fd)

class MutterParserTests(unittest.TestCase):
    def fixture(self):
        spec=('HDMI-2','vendor','model','serial')
        modes=[('m60',1920,1080,60.,1.,[1.],{'is-current':True}),
               ('m50',1920,1080,50.,1.,[1.],{})]
        return [1,[(spec,modes,{})],[(0,0,1.,0,True,[spec],{})],{'layout-mode':1}]
    def test_same_resolution_supported_targets_and_private_identity_hash(self):
        parsed=parse_mutter_state(self.fixture())
        self.assertEqual(parsed['supported'],{'50':'m50','60':'m60'})
        self.assertEqual(len(parsed['identity']),64)
        self.assertNotIn('serial',json.dumps(parsed))
    def test_layout_digest_ignores_refresh_but_detects_real_layout_change(self):
        state=self.fixture();before=parse_mutter_state(state)
        state[1][0][1][0][6]['is-current']=False;state[1][0][1][1][6]['is-current']=True
        after=parse_mutter_state(state)
        self.assertEqual(before['layout_digest'],after['layout_digest'])
        state[2][0]=(1,0,1.,0,True,state[2][0][5],{})
        self.assertNotEqual(parse_mutter_state(state)['layout_digest'],before['layout_digest'])
    def test_multiple_monitors_and_unrestorable_mode_are_refused(self):
        state=self.fixture();state[2].append(state[2][0])
        with self.assertRaises(ValueError):parse_mutter_state(state)
        state=self.fixture()
        state[1][0][1].insert(0,('other60',1920,1080,60.,1.,[1.],{}))
        with self.assertRaises(ValueError):parse_mutter_state(state)
    def test_unsupported_50_does_not_invent_a_mode_or_change_resolution(self):
        state=self.fixture();state[1][0][1].pop()
        self.assertEqual(parse_mutter_state(state)['supported'],{'60':'m60'})

if __name__=='__main__':unittest.main()
