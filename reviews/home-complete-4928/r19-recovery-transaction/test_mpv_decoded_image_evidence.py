import copy
import tempfile
import unittest
from pathlib import Path
from PIL import Image,PngImagePlugin
from mpv_decoded_image_evidence import MPVDecodedImageEvidence

class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'frames';self.t=10.;self.color='red';self.text='one'
        self.state={'mode':'mpv','seeking':False,'revision':1,'process':('boot',22,'10'),'path':'https://example.invalid/owned'}
        self.calls=[];self.hook=lambda:None
        self.reader=MPVDecodedImageEvidence(context=lambda:copy.deepcopy(self.state),command=self.command,root=self.root,clock=lambda:self.t)
    def command(self,parts,timeout):
        self.calls.append(parts)
        assert parts[0]=='screenshot-to-file' and parts[2]=='video'
        meta=PngImagePlugin.PngInfo();meta.add_text('comment',self.text)
        Image.new('RGB',(4,4),self.color).save(parts[1],pnginfo=meta)
        self.hook()
        return {'error':'success'}
    def test_only_actual_image_change_increments(self):
        a=self.reader.capture();self.t+=1
        b=self.reader.capture();self.color='blue';self.t+=1;c=self.reader.capture()
        self.assertEqual([a['frames'],b['frames'],c['frames']],[0,0,1])
        self.assertEqual(c['evidence_kind'],'decoded_image_changes')
        self.assertFalse(c['counter_is_total_decoded_frames'])
        self.assertEqual(list(self.root.iterdir()),[])
    def test_clock_motion_and_png_metadata_alone_never_count(self):
        a=self.reader.capture();self.text='different';self.t+=100
        self.assertEqual(self.reader.capture()['frames'],a['frames'])
    def test_process_source_revision_reset_observation_counter(self):
        for key,value in [('process',('boot',22,'11')),('path','https://example.invalid/new'),('revision',2)]:
            self.setUp();self.reader.capture();self.color='blue';self.t+=1
            self.assertEqual(self.reader.capture()['frames'],1)
            self.state[key]=value
            self.assertEqual(self.reader.capture()['frames'],0)
    def test_foreign_context_or_seek_change_during_capture_rejected(self):
        for key,value in [('process',('boot',99,'10')),('mode','kiosk'),('seeking',True),('revision',2)]:
            self.setUp();self.hook=lambda:self.state.update({key:value})
            self.assertIsNone(self.reader.capture())
            self.assertEqual(list(self.root.iterdir()),[])
    def test_no_capture_outside_mpv_or_during_seek(self):
        self.state['mode']='kiosk';self.assertIsNone(self.reader.capture());self.assertEqual(self.calls,[])
        self.state['mode']='mpv';self.state['seeking']=True;self.assertIsNone(self.reader.capture())
    def test_unapplied_screenshot_success_not_accepted(self):
        self.reader.command=lambda *a,**k:{'error':'success'}
        self.assertIsNone(self.reader.capture());self.assertEqual(list(self.root.iterdir()),[])
    def test_invalid_png_rejected_and_deleted(self):
        def invalid(parts,**kw):
            Path(parts[1]).write_bytes(b'not a png'*20);return {'error':'success'}
        self.reader.command=invalid
        self.assertIsNone(self.reader.capture());self.assertEqual(list(self.root.iterdir()),[])
    def test_slow_capture_rejected(self):
        self.hook=lambda:setattr(self,'t',self.t+4)
        self.assertIsNone(self.reader.capture())
    def test_exception_does_not_leak_or_poison_next_observation(self):
        self.reader.command=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('private URL'))
        self.assertIsNone(self.reader.capture())
        self.reader.command=self.command
        self.assertEqual(self.reader.capture()['frames'],0)
    def test_world_readable_capture_directory_refused(self):
        self.root.mkdir(mode=0o755)
        self.assertIsNone(self.reader.capture());self.assertEqual(self.calls,[])

if __name__=='__main__':unittest.main()
