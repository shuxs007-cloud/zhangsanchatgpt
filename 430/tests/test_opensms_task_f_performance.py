"""Offline regression checks for Task F batching and immutable-catalog reuse."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("task_f_db_performance", ROOT / "scripts/opensms_task_f_db.py")
DB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DB)


def catalog_row(number, activation="2026-08-03"):
    return {"ordinal": number + 1, "fields": {
        "lblPurchaseDate": "2026-08-01", "lblActivationRequestDate": "2026-08-03 09:00",
        "lblActivateDate": activation, "lblSimCardNumber": str(8912230200000000000 + number),
        "lblSKU": "TEST-SKU", "lblCost": "$12.00",
    }}


def save_catalog(path, rows):
    DB.atomic_json(path, {"status": "passed", "total": len(rows), "extractedRowCount": len(rows),
                          "rowCountMatchesTotal": True, "missingRequiredFields": [], "rows": rows})


class TaskFPerformanceTests(unittest.TestCase):
    def test_same_catalog_reuses_committed_validation_but_changed_bytes_revalidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, catalog = root / "db.sqlite", root / "catalog.json"
            rows = [catalog_row(0), catalog_row(1, "")]
            save_catalog(catalog, rows)
            first = DB.import_catalog(database, catalog, root / "first.json")
            self.assertEqual((first["foundCount"], first["activationMissingCount"]), (1, 1))
            with patch.object(DB, "parse_activation_date", side_effect=AssertionError("unchanged content reparsed")):
                second = DB.import_catalog(database, catalog, root / "second.json")
            self.assertTrue(second["alreadyImported"])
            self.assertTrue(second["normalizationSkippedForUnchangedCatalog"])
            self.assertEqual(first["sourceSha256"], second["sourceSha256"])
            save_catalog(catalog, rows + [rows[0]])
            with self.assertRaisesRegex(RuntimeError, "无效或重复"):
                DB.import_catalog(database, catalog, root / "invalid.json")
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM kooda_activation").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM grabandgo_catalog_import").fetchone()[0], 1)

    def test_only_current_iccids_are_pending_and_blank_catalog_dates_stay_known(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, catalog, cards = root / "db.sqlite", root / "catalog.json", root / "cards.json"
            save_catalog(catalog, [catalog_row(0), catalog_row(1, ""), catalog_row(2)])
            DB.import_catalog(database, catalog, root / "import.json")
            DB.atomic_json(cards, {"rows": [{"cardStore": str(8912230200000000000 + i) + "f", "opr": "Telus1"}
                                           for i in [0, 1, 3]]})
            result = DB.prepare_pending(database, cards, root / "pending.json")
            self.assertEqual(result["pendingCount"], 1)
            self.assertEqual(result["alreadyQueriedCount"], 2)
            self.assertEqual(result["rows"][0]["queryIccid"], "8912230200000000003")
            self.assertEqual(result["alreadyQueried"][1]["lookupStatus"], "activation_missing")
            self.assertFalse(any(row["queryIccid"] == "8912230200000000002" for row in result["alreadyQueried"]))

    def test_batched_selects_and_upsert_preserve_first_seen_and_latest_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, cards = root / "db.sqlite", root / "cards.json"
            rows = [{"cardStore": str(8912230200000000000 + i) + "F", "opr": "Kooda", "slot": "old"}
                    for i in range(1805)]
            DB.atomic_json(cards, {"rows": rows})
            with patch.object(DB, "now_iso", return_value="2026-09-01T00:00:00+08:00"):
                DB.prepare_pending(database, cards, root / "first.json")
            rows[0]["slot"] = "new"
            DB.atomic_json(cards, {"rows": rows})
            statements = []
            original_open = DB.open_database

            def tracked_open(path):
                connection = original_open(path)
                connection.set_trace_callback(statements.append)
                return connection

            with patch.object(DB, "open_database", side_effect=tracked_open), patch.object(
                DB, "now_iso", return_value="2026-09-02T00:00:00+08:00"
            ):
                result = DB.prepare_pending(database, cards, root / "second.json")
            activation_selects = [sql for sql in statements if sql.lstrip().startswith("SELECT") and "FROM kooda_activation" in sql]
            self.assertEqual(len(activation_selects), 3)
            self.assertEqual(result["databaseLookupBatchCount"], 3)
            self.assertFalse(any("SELECT first_seen_at" in sql for sql in statements))
            with sqlite3.connect(database) as connection:
                record = connection.execute("SELECT first_seen_at, last_seen_at, slot FROM kooda_inventory WHERE query_iccid=?",
                                            ("8912230200000000000",)).fetchone()
            self.assertEqual(record, ("2026-09-01T00:00:00+08:00", "2026-09-02T00:00:00+08:00", "new"))

    def test_export_serializes_once_and_publishes_identical_complete_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, catalog = root / "db.sqlite", root / "catalog.json"
            save_catalog(catalog, [catalog_row(0), catalog_row(1, "")])
            DB.import_catalog(database, catalog, root / "import.json")
            output, latest = root / "run/library.json", root / "state/latest.json"
            with patch.object(DB, "encode_json", wraps=DB.encode_json) as encoder:
                result = DB.export_catalog(database, output, latest)
            self.assertEqual(encoder.call_count, 1)
            self.assertEqual(output.read_bytes(), latest.read_bytes())
            self.assertEqual(json.loads(output.read_bytes()), result)
            self.assertEqual(result["rows"][0]["cutoffDate"], "2026-09-01")
            self.assertEqual(result["rows"][0]["sourceData"]["fields"]["lblSKU"], "TEST-SKU")
            self.assertEqual(result["rows"][1]["activationDate"], None)

    def test_empty_inventory_has_zero_database_lookup_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            DB.atomic_json(root / "cards.json", {"rows": []})
            result = DB.prepare_pending(root / "db.sqlite", root / "cards.json", root / "pending.json")
            self.assertEqual(result["databaseLookupBatchCount"], 0)
            self.assertEqual(result["pendingCount"], 0)

    def test_pending_cli_keeps_full_rows_in_receipt_and_only_summary_in_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cards, output = root / "cards.json", root / "pending.json"
            DB.atomic_json(cards, {"rows": [{"cardStore": "8912230200000000001F", "opr": "Kooda",
                                            "sourceData": {"auditField": "retained"}}]})
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/opensms_task_f_db.py"), "pending", "--db", str(root / "db.sqlite"),
                 "--cards", str(cards), "--output", str(output)],
                capture_output=True, text=True, check=True,
            )
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["pendingCount"], 1)
            self.assertTrue(summary["rowsOmittedFromStdout"])
            self.assertNotIn("rows", summary)
            self.assertEqual(summary["detailPath"], str(output.resolve()))
            self.assertEqual(DB.load_json(output)["rows"][0]["sourceData"]["auditField"], "retained")


if __name__ == "__main__":
    unittest.main()
