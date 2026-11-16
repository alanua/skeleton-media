import hashlib
import json
import unittest
from youtube_lounge_events import LoungeStream,LoungeProtocolError,receiver_request


def frame(messages):
    body=json.dumps(messages,ensure_ascii=False,separators=(',',':')).encode()
    return str(len(body)).encode()+b'\n'+body


class LoungeTests(unittest.TestCase):
    def test_fragmented_receiver_commands_and_nonmedia_messages(self):
        s=LoungeStream(10);wire=frame([[11,['remoteConnected',{'name':'private-person'}]],
            [12,['setPlaylist',{'videoIds':'AAAAAAAAAAA,BBBBBBBBBBB','currentIndex':'1','currentTime':'0'}]]])
        for byte in wire:s.feed(bytes([byte]))
        self.assertEqual(len(s.evidence),1);self.assertEqual(s.evidence[0].command,'setPlaylist')
        self.assertEqual(s.evidence[0].video_sha256,hashlib.sha256(b'BBBBBBBBBBB').hexdigest())
        self.assertNotIn('private-person',json.dumps(s.summary()))
    def test_same_video_replay_is_distinguished_by_sequence(self):
        s=LoungeStream(0);message=lambda seq:[seq,['setVideo',{'videoId':'AAAAAAAAAAA'}]]
        s.feed(frame([message(1),message(1),message(0),message(2)]))
        self.assertEqual([x.sequence for x in s.evidence],[1,2]);self.assertEqual(s.replayed,2)
    def test_unknown_initial_baseline_never_activates(self):
        s=LoungeStream();s.feed(frame([[1,['setVideo',{'videoId':'AAAAAAAAAAA'}]]]))
        self.assertEqual(s.evidence,[]);self.assertFalse(s.summary()['baseline_known'])
    def test_shape_diagnostics_only_export_fixed_field_types(self):
        s=LoungeStream(0);s.feed(frame([[1,['setPlaylist',{'videoId':'private-value','currentIndex':0,'currentTime':-1,'private-field':'private-token'}]]]))
        summary=s.summary();self.assertIn('setPlaylist:currentIndex:int',summary['media_shapes'])
        self.assertNotIn('private',json.dumps(summary));self.assertFalse(s.evidence)
    def test_pairing_heartbeat_playlist_update_and_status_are_not_media_loads(self):
        for command in ['c','S','noop','remoteConnected','loungeStatus','updatePlaylist','getNowPlaying','nowPlaying','onStateChange','play']:
            s=LoungeStream(-1);s.feed(frame([[0,[command,{'videoId':'AAAAAAAAAAA','token':'private'}]]]))
            self.assertEqual(s.evidence,[],command);self.assertNotIn('private',json.dumps(s.summary()))
    def test_invalid_selection_is_not_promoted(self):
        variants=[{}, {'videoId':'bad'}, {'videoId':True}, {'videoIds':'AAAAAAAAAAA','currentIndex':'1'},
            {'videoIds':'AAAAAAAAAAA','currentIndex':False},{'videoId':'BBBBBBBBBBB','videoIds':'AAAAAAAAAAA','currentIndex':'0'},
            {'videoId':'AAAAAAAAAAA','currentTime':'NaN'},{'videoId':'AAAAAAAAAAA','currentTime':-1}]
        for args in variants:
            s=LoungeStream(-1);s.feed(frame([[0,['setPlaylist',args]]]))
            self.assertEqual(s.evidence,[],args)
    def test_multiple_frames_utf8_lengths_and_unknown_commands_redacted(self):
        s=LoungeStream(0);s.feed(frame([[1,['secret-token-value',{'name':'Ім’я'}]]])+frame([[2,['setVideo',{'videoId':'AAAAAAAAAAA'}]]]))
        self.assertEqual(s.frames,2);self.assertEqual(s.commands,{'other':1,'setVideo':1})
        self.assertNotIn('secret-token-value',json.dumps(s.summary()))
    def test_malformed_sequence_or_shape_cannot_be_event(self):
        s=LoungeStream(0);s.feed(frame([[True,['setVideo',{'videoId':'AAAAAAAAAAA'}]],['1',['setVideo',{}]],{},[1,[]]]))
        self.assertEqual(s.evidence,[]);self.assertEqual(s.malformed,4)
    def test_bad_lengths_json_and_limits_fail_closed(self):
        for payload in [b'-1\n',b'9999999\n',b'2\n{}',b'4\nnope',b'xxxxxxxxx']:
            s=LoungeStream(0)
            with self.assertRaises(LoungeProtocolError):s.feed(payload)
            with self.assertRaises(LoungeProtocolError):s.feed(frame([[1,['setVideo',{'videoId':'AAAAAAAAAAA'}]]]))
        with self.assertRaises(LoungeProtocolError):LoungeStream(0,max_total=1).feed(b'22')
    def test_request_role_and_watermark_are_verified_without_exporting_tokens(self):
        value=receiver_request('https://www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN&AID=10&SID=private-session&loungeIdToken=private-token')
        self.assertEqual(value['watermark'],10);self.assertEqual(value['role'],'LOUNGE_SCREEN')
        self.assertEqual(len(value['session_digest']),64);self.assertNotIn('private',json.dumps(value))
    def test_untrusted_request_urls_roles_and_ambiguous_fields_rejected(self):
        for url in ['http://www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN',
            'https://evil.org/api/lounge/bc/bind?device=LOUNGE_SCREEN',
            'https://www.youtube.com/api/lounge/bc/bind?device=REMOTE_CONTROL',
            'https://www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN&device=REMOTE_CONTROL',
            'https://www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN&AID=1&AID=2',
            'https://www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN&AID=bad',
            'https://secret@www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN',
            'https://www.youtube.com/api/lounge/bc/bind?device=LOUNGE_SCREEN#private']:
            self.assertIsNone(receiver_request(url),url)


if __name__=='__main__':unittest.main()
