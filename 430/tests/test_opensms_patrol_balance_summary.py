import importlib.util
import sys
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "opensms_patrol_runner.py"
SPEC = importlib.util.spec_from_file_location("opensms_patrol_runner", RUNNER)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BalanceSummaryTests(unittest.TestCase):
    def test_compact_json_preserves_measured_balance_read_concurrency(self):
        measured = {"durationSeconds": 0.04, "configuredConcurrency": 4, "maxActive": 4}
        result = MODULE.compact_launcher_result({"status": "success", "performance": {"balanceStatisticsRead": measured}})
        self.assertEqual(result["performance"]["balanceStatisticsRead"], measured)

    def test_all_twelve_groups_are_always_returned_in_fixed_order(self):
        result = MODULE.build_balance_summary_groups({
            "records": [
                {
                    "group": "B1",
                    "dailyRemaining": 80,
                    "dailyTotal": 100,
                    "percentRemaining": 80.0,
                },
                {
                    "group": "C2",
                    "dailyRemaining": 0,
                    "dailyTotal": 0,
                    "percentRemaining": None,
                },
            ]
        })
        self.assertEqual(
            [item["group"] for item in result],
            ["A1", "A12", "A11", "B1", "B11", "B12", "C2", "A3", "B3", "B1V", "B11V", "B12V"],
        )
        self.assertEqual(result[3]["availability"], "available")
        self.assertEqual(result[3]["percentRemaining"], 80.0)
        self.assertEqual(result[6]["availability"], "unknown")
        self.assertIsNone(result[6]["percentRemaining"])
        self.assertEqual(sum(item["availability"] == "unknown" for item in result), 11)

    def test_b3_is_included_in_summary_contract_and_compact_json(self):
        groups = MODULE.build_balance_summary_groups({"records": [{
            "group": "B3", "dailyRemaining": 9, "dailyTotal": 100, "percentRemaining": 9.0,
        }]})
        self.assertEqual(groups[8]["group"], "B3")
        self.assertEqual(groups[8]["availability"], "available")
        self.assertEqual(groups[8]["percentRemaining"], 9.0)
        self.assertEqual(MODULE.execution_contract()["balanceSummaryGroups"][8], "B3")
        result = MODULE.compact_launcher_result({"status": "success", "balanceGroups": groups})
        self.assertEqual(result["balanceGroups"], groups)

    def test_v_groups_are_independent_and_preserved_in_compact_json(self):
        names = ("B1", "B11", "B12", "B1V", "B11V", "B12V")
        groups = MODULE.build_balance_summary_groups({"records": [
            {"group": name, "dailyRemaining": index + 1, "dailyTotal": 100,
             "percentRemaining": index + 1} for index, name in enumerate(names)]})
        by_group = {item["group"]: item for item in groups}
        for index, name in enumerate(names):
            self.assertEqual(by_group[name]["percentRemaining"], index + 1)
        self.assertEqual(MODULE.execution_contract()["balanceSummaryGroups"][-3:], list(names[-3:]))
        self.assertEqual(MODULE.compact_launcher_result({"status": "success", "balanceGroups": groups})["balanceGroups"], groups)


if __name__ == "__main__":
    unittest.main()
