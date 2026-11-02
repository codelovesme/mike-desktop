"""IDE context is refreshed locally, never a source of implicit tool grants."""
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client'))
import ide_context
from cli import Terminal


class Service:
    def call(self, path, body):
        self.path, self.body = path, body
        return {'turn_id': 'test'}


class IdeContextTest(unittest.TestCase):
    def test_outside_ide_has_no_added_context(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ide_context.current(), '')

    def test_missing_or_dead_ide_never_claims_a_different_connection(self):
        with patch.dict(os.environ, {'CODELOVESME_IDE_SESSION_FILE':'/missing'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                ide_context.current()
        env = dict(CODELOVESME_IDE_SESSION_FILE='/private/session.json',
                   CODELOVESME_IDE_HELPER='/app/ide_control.py', CODELOVESME_IDE_TERMINAL='terminal-2')
        with patch.dict(os.environ, env, clear=True), patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'closed')):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                ide_context.current()

    def test_model_gets_context_but_screen_gets_the_user_message(self):
        service = Service()
        terminal = Terminal(service, '/project', True, io.StringIO())
        with patch('ide_context.current', side_effect=['IDE session first; command /local/control state', 'IDE session second']):
            terminal.ask('move my terminal left')
            self.assertIn('IDE session first', service.body['text'])
            self.assertTrue(service.body['text'].endswith('move my terminal left'))
            self.assertEqual(service.body['workspace'], '/project')
            terminal.finished = True
            terminal.ask('where am I?')
            self.assertIn('IDE session second', service.body['text'])
            self.assertNotIn('first', service.body['text'])

    def test_oversized_context_cannot_silently_truncate_user_message(self):
        terminal = Terminal(Service(), '/project', True, io.StringIO())
        with patch('ide_context.current', return_value='x' * 1000):
            with self.assertRaisesRegex(ValueError, 'context exceeds'):
                terminal.ask('a' * 16000)
            self.assertEqual(terminal.turn, '')


if __name__ == '__main__':
    unittest.main()
