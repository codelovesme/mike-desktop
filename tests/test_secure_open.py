import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("secure_open", Path(__file__).resolve().parents[1] / "ui/secure_open.py")
secure_open = importlib.util.module_from_spec(spec)
spec.loader.exec_module(secure_open)


class DescriptorScopeTest(unittest.TestCase):
    def test_exact_regular_file_is_opened_by_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "chosen"
            (root / "inside").mkdir(parents=True)
            file = root / "inside" / "invoice.txt"
            file.write_text("reviewed")
            descriptor = secure_open.open_verified(str(root), str(file))
            try:
                self.assertEqual(os.read(descriptor, 100), b"reviewed")
                self.assertEqual(os.fstat(descriptor).st_ino, file.stat().st_ino)
            finally:
                os.close(descriptor)

    def test_symlink_replacement_and_escape_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            area = Path(temporary)
            root = area / "chosen"
            root.mkdir()
            outside = area / "outside.txt"
            outside.write_text("secret")
            candidate = root / "invoice.txt"
            candidate.write_text("reviewed")
            candidate.unlink()
            candidate.symlink_to(outside)
            with self.assertRaises(OSError):
                secure_open.open_verified(str(root), str(candidate))
            (root / "link").symlink_to(area, target_is_directory=True)
            with self.assertRaises(OSError):
                secure_open.open_verified(str(root), str(root / "link" / "outside.txt"))
            with self.assertRaises(ValueError):
                secure_open.open_verified(str(root), str(outside))


if __name__ == "__main__":
    unittest.main()
