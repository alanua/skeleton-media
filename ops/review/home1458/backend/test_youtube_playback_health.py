import unittest
from youtube_playback_health import youtube_progress_health as health


class HealthTests(unittest.TestCase):
    def sample(self,**values):
        return dict({'running':True,'pause':False,'video_id':'synthetic','time-pos':10,
                     'decoded_frames':100,'ready_state':4,'video_width':1280,
                     'playback_rate':1,'seeking':False},**values)
    def test_playing_requires_clock_and_frames(self):
        self.assertEqual(health(self.sample(),self.sample(**{'time-pos':14,'decoded_frames':200}),4),{'state':'playing','healthy':True})
    def test_ready_state_four_frozen_is_not_healthy(self):
        self.assertEqual(health(self.sample(),self.sample(),4),{'state':'stalled','healthy':False})
    def test_time_advance_with_frozen_frame_is_not_healthy(self):
        self.assertEqual(health(self.sample(),self.sample(**{'time-pos':14}),4)['state'],'frame_stalled')
    def test_pause_is_not_a_failure_to_recover(self):
        self.assertEqual(health(self.sample(),self.sample(pause=True),4)['state'],'paused')
    def test_seek_clip_change_counter_reset_are_transitions(self):
        for change in [{'seeking':True},{'video_id':'new'},{'decoded_frames':2},{'time-pos':1},{'time-pos':200}]:
            self.assertEqual(health(self.sample(),self.sample(**change),4)['state'],'transition')
    def test_buffering_is_not_healthy(self):
        self.assertEqual(health(self.sample(),self.sample(ready_state=2),4)['state'],'buffering')
    def test_missing_frame_evidence_is_unverified(self):
        self.assertEqual(health(self.sample(),self.sample(**{'time-pos':14,'decoded_frames':None}),4)['state'],'unverified')
    def test_audio_only_can_advance_without_video(self):
        self.assertTrue(health(self.sample(video_width=0),self.sample(**{'time-pos':14,'video_width':0,'decoded_frames':None}),4)['healthy'])
    def test_stopped_or_short_or_invalid_probe_is_not_healthy(self):
        self.assertEqual(health(self.sample(),self.sample(running=False),4)['state'],'stopped')
        for elapsed in [0,1,None,float('nan'),True]:self.assertFalse(health(self.sample(),self.sample(),elapsed)['healthy'])
        self.assertFalse(health(None,self.sample(),4)['healthy'])


if __name__=='__main__':unittest.main()
