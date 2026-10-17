import sys
import tempfile
import unittest
import os
import json
import shutil
import subprocess
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from opensms_selected_phone_report import (
    phone_key, read_phones, SelectedPhoneRunner, PatrolError,
    extract_main, verify_selected, value_signature, NODE, NODE_MODULES, SCRIPTS,
)


class SelectedPhoneTests(unittest.TestCase):
    def test_exact_phone(self):
        for value in ("7052088365", 7052088365, 7052088365.0, "17052088365", "+17052088365"):
            self.assertEqual(phone_key(value), "7052088365")

    def test_never_substring_or_foreign_prefix(self):
        for value in ("xx7052088365", "27052088365", "705208836500", "705208836", 7052088365.4, True, None):
            self.assertIsNone(phone_key(value))

    def test_list_order_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "input.txt"
            p.write_text("7052088365\n4389365601\n7052088365\n")
            self.assertEqual(read_phones(p), (["7052088365", "4389365601"], 3))

    def test_invalid_list_stops(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "input.txt"
            for content in ("", "7052088365\ninvalid\n", "+17052088365\n"):
                p.write_text(content)
                with self.assertRaises(ValueError):
                    read_phones(p)

    def test_business_and_f_hard_blocks(self):
        runner = object.__new__(SelectedPhoneRunner)
        for name in ("execute", "run_write", "run_standard_writes", "start_background_task_f"):
            with self.assertRaises(PatrolError):
                getattr(runner, name)()

    def test_exact_excel_numeric_semantics(self):
        old = SimpleNamespace(value=8.93027205368282e19, data_type="n")
        new = SimpleNamespace(value=89302720536828200000, data_type="n")
        wrong = SimpleNamespace(value=89302720536828200001, data_type="n")
        self.assertEqual(value_signature(old), value_signature(new))
        self.assertNotEqual(value_signature(old), value_signature(wrong))
        self.assertNotEqual(value_signature(new), value_signature(SimpleNamespace(value=str(new.value), data_type="s")))

    @unittest.skipUnless(os.environ.get("OPENSMS_SELECTED_TEST_REFERENCE"), "optional offline format roundtrip")
    def test_artifact_roundtrip(self):
        source = Path(os.environ["OPENSMS_SELECTED_TEST_REFERENCE"])
        phones = ["7052088365", "4389365601", "6727556498", "0000000000"]
        if os.environ.get("OPENSMS_SELECTED_TEST_PHONES"):
            phones = read_phones(os.environ["OPENSMS_SELECTED_TEST_PHONES"])[0] + ["0000000000"]
        packed = extract_main(source, phones)
        with tempfile.TemporaryDirectory(prefix="selected-phone-check-", dir="/Volumes/zs01/chatgpt/OpenSMS/local-work/outputs") as td:
            root = Path(td)
            (root / "node_modules").symlink_to(NODE_MODULES, target_is_directory=True)
            builder = root / "build.mjs"
            shutil.copy2(SCRIPTS / "opensms_selected_phone_report.mjs", builder)
            paths = {"sourceWorkbook": str(root / "source.xlsx"), "finalWorkbook": str(root / "processed.xlsx")}
            spec = root / "spec.json"
            spec.write_text(json.dumps({"phones": phones, "source": packed, "processed": packed, "outputs": paths}, ensure_ascii=False))
            subprocess.run([str(NODE), str(builder), str(spec)], check=True, capture_output=True, text=True)
            for key, check_sheet in (("sourceWorkbook", False), ("finalWorkbook", True)):
                proof = verify_selected(source, paths[key], phones, packed["sourceSha256"], packed["counts"], check_sheet)
                self.assertTrue(proof["independentReadbackPassed"])
                self.assertEqual(proof["outOfScopeRows"], 0)


if __name__ == "__main__":
    unittest.main()
