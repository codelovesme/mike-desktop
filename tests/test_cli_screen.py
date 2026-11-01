"""Mike CLI's full-screen chat, without a terminal: the editor, the keys,
the text Mike's output may carry, voice and images."""
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'client'))
import screen
from screen import Editor, parse_keys, markdown, wrap_spans, visible_width, read_history, write_history


class EditorTest(unittest.TestCase):
    def test_multiline_and_caret_moves(self):
        e = Editor()
        e.insert('one')
        e.insert('\n')
        e.insert('two')
        self.assertEqual(e.text, 'one\ntwo')
        self.assertTrue(e.vertical(40, -1))
        self.assertEqual(e.caret, 3)
        self.assertFalse(e.vertical(40, -1))
        e.home(); self.assertEqual(e.caret, 0)
        e.end(); self.assertEqual(e.caret, 3)

    def test_wrapped_rows(self):
        e = Editor()
        e.insert('abcdefghij')
        rows, r, col = e.where(4)
        self.assertEqual([(s, t) for s, t, _ in rows], [(0, 4), (4, 8), (8, 10)])
        self.assertEqual((r, col), (2, 2))

    def test_history_keeps_the_draft(self):
        e = Editor(['old'])
        e.insert('draft')
        e.older(); self.assertEqual(e.text, 'old')
        e.newer(); self.assertEqual(e.text, 'draft')

    def test_image_is_one_token(self):
        e = Editor()
        e.add_image('image/png', b'x')
        self.assertEqual(e.text, '[Image #1]')
        self.assertEqual(e.images_used(), [1])
        e.backspace()
        self.assertEqual(e.text, '')
        self.assertEqual(e.images_used(), [])

    def test_kill_word_and_line(self):
        e = Editor()
        e.insert('hello big world')
        e.kill_word(); self.assertEqual(e.text, 'hello big ')
        e.kill_before(); self.assertEqual(e.text, '')

    def test_history_file_round_trip_and_old_format(self):
        path = Path(tempfile.mkdtemp()) / 'h'
        path.write_text('plain old line\n')
        self.assertEqual(read_history(path), ['plain old line'])
        write_history(path, ['a\nb', 'c'])
        self.assertEqual(read_history(path), ['a\nb', 'c'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class KeysTest(unittest.TestCase):
    def test_keys(self):
        events, rest = parse_keys('hi\r\x1b[A\x1b\r\n\x1b[13;2u\x7f\x16', final=True)
        self.assertEqual(rest, '')
        self.assertEqual(events, [('text', 'hi'), ('key', 'enter'), ('key', 'up'), ('key', 'newline'),
                                  ('key', 'newline'), ('key', 'newline'), ('key', 'backspace'), ('key', 'ctrl-v')])

    def test_push_to_talk_events_and_fragmented_release(self):
        data = '\x1b[?11u\x1b[32;5u\x1b[32;5:2u\x1b[32;1:3u\x1b[57442;1:3u'
        events, rest = parse_keys(data)
        self.assertEqual(rest, '')
        self.assertEqual(events, [('keyboard', '11'), ('key', 'ctrl-space'),
            ('key-repeat', 'ctrl-space'), ('key-up', 'space'), ('key-up', 'control')])
        events, rest = parse_keys('\x1b[32;5:')
        self.assertEqual(events, [])
        self.assertEqual(parse_keys(rest+'3u')[0], [('key-up', 'space')])
        self.assertEqual(parse_keys('\x1b[97;1u\x1b[97;1:3u')[0], [('text', 'a')])
        self.assertEqual(parse_keys('\x1b[1;1:3A')[0], [])

    def test_paste_keeps_newlines_and_waits_for_its_end(self):
        events, rest = parse_keys('\x1b[200~a\rb')
        self.assertEqual(events, [])
        events, rest = parse_keys(rest + '\x1b[201~')
        self.assertEqual(events, [('paste', 'a\rb')])

    def test_lone_escape_waits_unless_final(self):
        self.assertEqual(parse_keys('\x1b'), ([], '\x1b'))
        self.assertEqual(parse_keys('\x1b', final=True), ([('key', 'esc')], ''))


class TextTest(unittest.TestCase):
    def test_wrap_fits_width(self):
        for line in wrap_spans([('word ' * 40, '')], 30, '  '):
            self.assertLessEqual(visible_width(line), 30)

    def test_markdown(self):
        lines = markdown('# Title\n- **bold** and `code`\n```\nx = 1\n```', 60)
        flat = [screen.re.sub(r'\x1b\[[0-9;]*m', '', l) for l in lines]
        self.assertEqual(flat, ['  Title', '  • bold and code', '    x = 1'])

    def test_control_sequences_from_mike_are_removed(self):
        self.assertEqual(screen.safe('a\x1b[2Jb\x07'), 'a[2Jb')


class FakeTerminal:
    owner, device, workspace = 'ada@example.invalid', 'd1', '/project'
    turn, finished, view, local, waiting, status, started = '', False, {}, None, [], {}, 0.0

    def __init__(self):
        self.lines = []
        self.service = self

    def call(self, route, body, timeout=20):
        assert route == '/v1/chat/hear' and body['format'] == 'wav'
        return {'text': 'hello\x1b[2J  there'}

    def command(self, line):
        self.lines.append(line)

    def running(self): return False
    def doing(self): return ''


class ScreenTest(unittest.TestCase):
    def make(self):
        s = screen.Screen(FakeTerminal(), None, fd_in=0, out=io.StringIO())
        s.size = lambda: (80, 24)
        return s

    def test_voice_text_lands_in_the_box_sanitized(self):
        s = self.make()
        s.transcribe(b'RIFF')
        kind, words = s.heard.get_nowait()
        self.assertEqual(kind, 'text')
        s.editor.insert(screen.safe(words))
        self.assertEqual(s.editor.text, 'hello[2J  there')

    def test_damaged_image_is_refused_without_losing_the_draft(self):
        s = self.make()
        s.editor.add_image('image/png', b'x')
        s.submit()
        self.assertEqual(s.terminal.lines, [])
        self.assertIn('[Image #1]', s.editor.text)
        self.assertIn('image content is not a PNG', s.out.getvalue())

    def test_slash_menu_enter_completes_a_command_that_needs_words(self):
        s = self.make()
        s.handle('text', '/res')
        s.handle('key', 'enter')
        self.assertEqual(s.editor.text, '/resume ')
        self.assertEqual(s.terminal.lines, [])

    def test_question_needs_a_visible_choice(self):
        s = self.make()
        t = s.terminal
        t.turn, t.view = 't1', {'stage': 'confirm', 'confirmation_id': 'q' * 64, 'say': 'ok?'}
        s.live(80)
        s.handle('key', 'enter')
        self.assertEqual(t.lines, [])
        s.handle('text', '2')
        s.handle('key', 'enter')
        self.assertEqual(t.lines, ['/no ' + 'q' * 12])

    def test_hold_release_records_once_and_preserves_the_draft(self):
        for released in ('space', 'control'):
            s = self.make()
            s.handle('keyboard', '11')
            s.editor.set('draft ')
            recorder = Mock(process=None)
            def start(): recorder.process = object(); return True
            def stop(): recorder.process = None; return b'RIFF'
            recorder.start.side_effect = start
            recorder.stop.side_effect = stop
            s.recorder = recorder
            with patch.object(screen.Recorder, 'available', return_value=True):
                s.handle('key', 'ctrl-space')
                s.handle('key-repeat', 'ctrl-space')
                s.handle('key', 'ctrl-space')
                self.assertEqual(recorder.start.call_count, 1)
                recorder.stop.assert_not_called()
                self.assertEqual(s.editor.text, 'draft ')
                s.handle('key-up', released)
                kind, words = s.heard.get(timeout=2)
                self.assertEqual(kind, 'text')
                self.assertIn('hello', words)
                s.handle('key-up', 'space')
                self.assertEqual(recorder.stop.call_count, 1)

    def test_no_release_support_does_not_open_the_microphone(self):
        s = self.make()
        with patch.object(s.recorder, 'start') as start:
            s.handle('key', 'ctrl-space')
            start.assert_not_called()
        self.assertIn('key releases', s.flash)

    def test_no_recorder_says_so(self):
        s = self.make()
        old = screen.shutil.which
        screen.shutil.which = lambda name: None
        try:
            s.voice()
        finally:
            screen.shutil.which = old
        self.assertIn('No recorder', s.flash)


if __name__ == '__main__':
    unittest.main()
