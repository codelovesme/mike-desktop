import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('account_session', Path(__file__).resolve().parents[1] / 'service/adapters/account_session.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AccountStoreTest(unittest.TestCase):
    def test_atomic_private_roundtrip_replacement_and_removal(self):
        with tempfile.TemporaryDirectory() as d:
            area = Path(d)
            value = {'host': 'https://apps.codeloves.me', 'token': 'private-token'}
            module.store(area, 'write', value)
            self.assertEqual(module.store(area, 'read'), value)
            target = area / 'account-session.json'
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            module.store(area, 'write', {'token': 'replacement'})
            self.assertEqual(module.store(area, 'read')['token'], 'replacement')
            self.assertEqual(sorted(p.name for p in area.iterdir()), ['account-session.json'])
            module.store(area, 'remove')
            self.assertEqual(module.store(area, 'read'), {})

    def test_unsafe_files_and_directories_are_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            area = Path(d); target = area / 'account-session.json'
            target.write_text('{"token":"secret"}'); target.chmod(0o644)
            with self.assertRaises(ValueError): module.store(area, 'read')
            target.unlink(); outside = area / 'outside'; outside.write_text('unchanged')
            target.symlink_to(outside)
            with self.assertRaises(OSError): module.store(area, 'read')
            module.store(area, 'write', {'token': 'safe'})
            self.assertEqual(outside.read_text(), 'unchanged')
            area.chmod(0o755)
            with self.assertRaises(ValueError): module.store(area, 'read')

    def test_remove_unlinks_a_symlink_and_oversized_records_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            area = Path(d); target = area / 'account-session.json'
            outside = area / 'outside'; outside.write_text('unchanged')
            target.symlink_to(outside); module.store(area, 'remove')
            self.assertEqual(outside.read_text(), 'unchanged')
            with self.assertRaises(ValueError): module.store(area, 'write', {'token': 'x' * module.LIMIT})
            self.assertFalse(target.exists())
