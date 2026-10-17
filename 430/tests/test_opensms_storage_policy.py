import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("storage", Path(__file__).resolve().parents[1] / "scripts/opensms_storage_policy.py")
storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage)


class StorageTests(unittest.TestCase):
    def test_copy_verify_link_and_idempotence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source, target = root / "source", root / "disk/target"
            source.mkdir()
            (source / "source.xlsx").write_bytes(b"exact bytes\x00")
            expected = storage.manifest(source)
            result = storage.migrate_directory(source, target, root / "receipts")
            self.assertEqual(result["status"], "verified")
            self.assertTrue(source.is_symlink())
            self.assertEqual(storage.manifest(target), expected)
            self.assertEqual(storage.migrate_directory(source, target, root / "receipts")["status"], "already_migrated")

    def test_existing_target_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source, target = root / "source", root / "target"
            source.mkdir(); target.mkdir()
            with self.assertRaises(RuntimeError):
                storage.migrate_directory(source, target, root / "receipts")
            self.assertFalse(source.is_symlink())

    def test_wrong_link_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "source"
            source.symlink_to(root / "other")
            with self.assertRaises(RuntimeError):
                storage.migrate_directory(source, root / "target", root / "receipts")
            self.assertTrue(source.is_symlink())

    def test_source_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = root / "source"; source.mkdir()
            (source / "nested").symlink_to(root)
            with self.assertRaises(RuntimeError):
                storage.migrate_directory(source, root / "target", root / "receipts")
            self.assertFalse((root / "target").exists())

    def test_failure_keeps_complete_source(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = root / "source"; source.mkdir()
            (source / "a").write_text("original")
            with patch.object(storage.shutil, "copytree", side_effect=OSError("disk removed")):
                with self.assertRaises(OSError):
                    storage.migrate_directory(source, root / "target", root / "receipts")
            self.assertEqual((source / "a").read_text(), "original")

    def test_external_input_root_keeps_exact_run_scope(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            job = root / "jobs/20260925T225300+0800"; job.mkdir(parents=True)
            settings = {"bulkRoot": str(root / "external")}
            with patch.object(storage, "config", return_value=settings), patch.object(storage, "require_external"):
                inputs = storage.input_directory(job, root)
                self.assertTrue(inputs.is_symlink())
                self.assertEqual(storage.verified_input_root(job, root), (root / "external/task_f_inputs" / job.name).resolve())
                inputs.unlink()
                inputs.symlink_to(root)
                with self.assertRaises(RuntimeError):
                    storage.verified_input_root(job, root)

    def test_exfat_sidecars_do_not_hide_changed_payload(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = root / "source"; source.mkdir()
            (source / "a").write_text("original")
            target = root / "target"; target.mkdir()
            (target / "a").write_text("original")
            (target / "._a").write_bytes(b"\x00\x05\x16\x07" + b"metadata")
            self.assertTrue(storage.verify_copy(target, storage.manifest(source)))
            (target / "a").write_text("changed")
            self.assertFalse(storage.verify_copy(target, storage.manifest(source)))
            (target / "a").write_text("original")
            result = storage.migrate_directory(source, target, root / "receipts", True)
            self.assertEqual(result["status"], "verified")

    def test_new_f_outputs_are_external_and_never_reused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            job = root / "jobs/20260925T225300+0800"; job.mkdir(parents=True)
            settings = {"bulkRoot": str(root / "external")}
            with patch.object(storage, "config", return_value=settings), patch.object(storage, "require_external"):
                target = storage.output_directory(job, root)
                self.assertEqual(target, root / "external/task_f_outputs" / job.name)
                self.assertFalse((job / "outputs").exists())
                with self.assertRaises(FileExistsError):
                    storage.output_directory(job, root)

    def test_missing_external_volume_prevents_f_output_creation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            job = root / "jobs/20260925T225300+0800"; job.mkdir(parents=True)
            settings = {"bulkRoot": str(root / "external")}
            with patch.object(storage, "config", return_value=settings), patch.object(storage, "require_external", side_effect=RuntimeError("disk missing")):
                with self.assertRaises(RuntimeError):
                    storage.output_directory(job, root)
            self.assertFalse((root / "external").exists())


if __name__ == "__main__":
    unittest.main()
