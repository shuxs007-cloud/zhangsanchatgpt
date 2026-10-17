import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from opensms_parallel_audit import PathResolver


class NestedReceiptsTests(unittest.TestCase):
    def test_exact_nested_receipt_wins_over_old_same_basename(self):
        with tempfile.TemporaryDirectory() as folder:
            delivery = Path(folder) / "run"
            nested = delivery / "receipts" / "attempt" / "receipt.json"
            nested.parent.mkdir(parents=True)
            nested.write_text('{"status":"passed"}')
            old = delivery / "receipts" / "receipt.json"
            old.write_text('{"status":"failed"}')
            resolver = PathResolver(delivery / "config.json", delivery)
            self.assertEqual(resolver.resolve(nested), nested.resolve())
            self.assertEqual(resolver.resolve(old), old.resolve())

    def test_relocated_copy_still_wins_over_external_old_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "old" / "receipt.json"
            old.parent.mkdir(); old.write_text("old")
            delivery = root / "new"
            copy = delivery / "receipts" / "receipt.json"
            copy.parent.mkdir(parents=True); copy.write_text("copy")
            self.assertEqual(PathResolver(delivery / "config.json", delivery).resolve(old), copy.resolve())


if __name__ == "__main__":
    unittest.main()
