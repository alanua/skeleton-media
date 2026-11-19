import json
from pathlib import Path
import tempfile
import unittest
from youtube_lounge_capture import capture
from test_youtube_lounge_events import frame
from test_youtube_lounge_xhr_observer import snapshot,load


class FakeClock:
    def __init__(self):self.value=0
    def now(self):return self.value
    def sleep(self,seconds):self.value+=seconds


class FakeReader:
    def __init__(self,items):self.items=items;self.index=0;self.closed=False
    def snapshot(self):
        value=self.items[min(self.index,len(self.items)-1)];self.index+=1
        if isinstance(value,Exception):raise value
        return value
    def close(self):self.closed=True


class CaptureTests(unittest.TestCase):
    def run_capture(self,reader,seconds=5):
        c=FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'result.json';value=capture(path,seconds,lambda:reader,c.now,c.sleep)
            self.assertEqual(json.loads(path.read_text()),value);self.assertEqual(path.stat().st_mode&0o777,0o600)
            return value
    def test_no_cast_ends_at_bound_and_releases_reader(self):
        reader=FakeReader([[snapshot(frame([[1,['noop']]]))]])
        value=self.run_capture(reader);self.assertEqual(value['status'],'completed_without_fresh_media_load');self.assertEqual(value['elapsed_seconds'],5)
        self.assertTrue(reader.closed);self.assertFalse(value['media_input_sent'])
    def test_fresh_cast_ends_early_but_old_body_does_not(self):
        initial=frame([load(1)]);reader=FakeReader([[snapshot(initial)],[snapshot(initial+frame([load(2)]))]])
        value=self.run_capture(reader);self.assertEqual(value['status'],'fresh_media_load_observed');self.assertEqual(value['elapsed_seconds'],2)
        self.assertTrue(reader.closed);self.assertFalse(value['automatic_mode_change_enabled'])
    def test_error_does_not_export_private_details_and_closes(self):
        reader=FakeReader([RuntimeError('private-token')]);value=self.run_capture(reader)
        self.assertEqual(value['status'],'failed');self.assertEqual(value['error_type'],'RuntimeError');self.assertNotIn('private-token',json.dumps(value));self.assertTrue(reader.closed)


if __name__=='__main__':unittest.main()
