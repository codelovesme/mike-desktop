"""Voice mode transitions, stale callbacks and silence/audio boundaries."""
import base64
import io
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import wave

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'client'))
import screen
from voice_audio import Detector, IDLE_SECONDS, Player
from test_cli_screen import FakeTerminal


class VoiceModes(unittest.TestCase):
    def make(self):
        t=FakeTerminal();t.status={'voice_chat':True};t.turn='';t.finished=False;t.view={};t.local=None
        s=screen.Screen(t,fd_in=0,out=io.StringIO());s.size=lambda:(80,24);s.key_releases=True
        r=Mock(process=None,path='unused.wav')
        def start():r.process=True;return True
        def stop():r.process=None;return b'RIFF'
        r.start.side_effect=start;r.stop.side_effect=stop;r.seconds.return_value=0
        s.recorder=r
        s.player=Mock(process=None)
        s.terminal.ask=Mock()
        return s

    def available(self):
        return patch.multiple(screen.Recorder,available=Mock(return_value=True))

    def transcript(self,s):
        kind,payload=s.heard.get(timeout=2);s.audio_event(kind,payload)

    def test_tap_latches_and_each_stopping_key_only_transcribes(self):
        for kind,value in [('key','ctrl-space'),('text',' '),('key','enter')]:
            s=self.make();s.editor.set('draft ')
            with self.available():
                s.handle('key','ctrl-space');s.handle('key-up','space')
                self.assertEqual(s.record_mode,'latched')
                s.recorder.stop.assert_not_called()
                s.handle(kind,value)
                self.transcript(s)
            self.assertIn('draft hello',s.editor.text)
            s.terminal.ask.assert_not_called()
            self.assertEqual(s.terminal.lines,[])
            self.assertEqual(s.recorder.stop.call_count,1)

    def test_stopping_enter_is_not_replayed_while_transcribing(self):
        s=self.make();s.transcribing=True;s.editor.set('draft')
        s.handle('key','enter')
        self.assertEqual(s.editor.text,'draft');self.assertEqual(s.terminal.lines,[])

    def test_both_conversation_shortcuts_stop_and_repeats_do_not_toggle(self):
        for shortcut in ('ctrl-space','voice-chat'):
            s=self.make()
            with self.available(),patch.object(Player,'available',return_value=True):
                s.handle('key','voice-chat');self.assertTrue(s.conversation)
                s.handle('key-repeat','voice-chat');self.assertTrue(s.conversation)
                s.handle('key',shortcut);self.assertFalse(s.conversation)

    def test_starting_conversation_does_not_discard_a_manual_recording(self):
        s=self.make();s.recorder.process=True;s.record_mode='latched'
        s.start_conversation()
        self.assertFalse(s.conversation);s.recorder.stop.assert_not_called()
        self.assertEqual(s.record_mode,'latched')

    def test_stopping_discards_late_transcription_and_late_speech(self):
        s=self.make();s.conversation=True;s.audio_epoch=4
        s.stop_conversation()
        s.audio_event('transcript',{'epoch':4,'conversation':True,'question':None,'text':'run something'})
        s.audio_event('speech',{'epoch':4,'sound':{'audio_base64':'UklGRg==','format':'wav'}})
        s.terminal.ask.assert_not_called();s.player.start.assert_not_called()
        self.assertEqual(s.editor.text,'')

    def test_listen_send_speak_then_listen_without_capturing_playback(self):
        s=self.make()
        def call(route,body,timeout=20):
            if route.endswith('/hear'):return {'text':'hello'}
            if route.endswith('/read'):return {'reading_id':'r1'}
            return {'stage':'ready','audio_base64':base64.b64encode(b'RIFF').decode(),'format':'wav'}
        s.terminal.call=call
        def ask(text):s.terminal.turn='t1';s.terminal.finished=False;s.terminal.view={'stage':'plan'}
        s.terminal.ask.side_effect=ask
        with self.available(),patch.object(Player,'available',return_value=True):
            s.start_conversation();s.audio_tick();self.assertTrue(s.recorder.process)
            s.detector=Mock();s.detector.inspect.return_value='utterance'
            s.audio_tick();self.transcript(s)
            s.terminal.ask.assert_called_once_with('hello')
            self.assertFalse(s.recorder.process)
            s.terminal.finished=True;s.terminal.view={'stage':'done','reply':'Hello back'}
            s.audio_tick()
            proc=Mock();proc.poll.return_value=None
            s.player.start.side_effect=lambda *a:setattr(s.player,'process',proc)
            s.player.stop.side_effect=lambda:setattr(s.player,'process',None)
            self.transcript(s)  # speech callback
            s.audio_tick();self.assertFalse(s.recorder.process)
            proc.poll.return_value=0
            s.audio_tick();self.assertTrue(s.recorder.process)
            self.assertEqual(s.conversation_phase,'listening')
            s.stop_conversation()

    def test_idle_and_focus_loss_close_recording_without_sending(self):
        for stop in ('idle','focus'):
            s=self.make()
            with self.available(),patch.object(Player,'available',return_value=True):
                s.start_conversation();s.audio_tick()
                if stop=='idle':
                    s.detector=Mock();s.detector.inspect.return_value='idle';s.audio_tick()
                else:s.handle('key-up','escape')
            self.assertFalse(s.conversation);self.assertFalse(s.recorder.process)
            s.terminal.ask.assert_not_called();self.assertFalse(s.transcribing)

    def test_spoken_yes_applies_only_to_the_displayed_question(self):
        s=self.make();s.conversation=True;s.audio_epoch=1
        s.terminal.turn='t1';s.terminal.view={'stage':'confirm','confirmation_id':'a'*64,'say':'Do this?'}
        context={'epoch':1,'conversation':True,'question':'ask:'+'a'*12}
        s.audio_event('transcript',dict(context,text='Do it'))
        self.assertEqual(s.terminal.lines,[])
        s.audio_event('transcript',dict(context,text='Evet.'))
        self.assertEqual(s.terminal.lines,['/yes '+'a'*12])

    def test_voice_cannot_approve_a_replaced_question(self):
        s=self.make();s.conversation=True;s.audio_epoch=1
        s.terminal.turn='t1';s.terminal.view={'stage':'confirm','confirmation_id':'b'*64,'say':'new question'}
        s.audio_event('transcript',{'epoch':1,'conversation':True,'question':'ask:'+'a'*12,'text':'yes'})
        self.assertEqual(s.terminal.lines,[]);self.assertFalse(s.conversation)


class SilenceTest(unittest.TestCase):
    def test_live_zero_length_header_and_silence_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'input.wav'
            samples=struct.pack('<1600h',*([4000]*1600))
            with wave.open(str(p),'wb') as w:
                w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(samples)
            data=bytearray(p.read_bytes());data[4:8]=b'\0'*4;data[40:44]=b'\0'*4;p.write_bytes(data)
            detector=Detector();start=detector.started
            self.assertEqual(detector.inspect(p,start),'')
            self.assertTrue(detector.speech)
            self.assertEqual(detector.inspect(p,start+1.3),'utterance')
            detector=Detector();p.write_bytes(b'')
            self.assertEqual(detector.inspect(p,detector.started+IDLE_SECONDS+1),'idle')

if __name__=='__main__':unittest.main()
