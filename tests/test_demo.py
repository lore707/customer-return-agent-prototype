import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import database
import demo


class PortfolioDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "portfolio.db"
        database.init_database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_showcase_has_six_traceable_scenarios(self):
        cases = demo.ensure_showcase(path=self.db_path)
        self.assertEqual(6, len(cases))
        self.assertTrue(all(case["scenario_slug"] for case in cases))
        self.assertTrue(all(case["source_mode"] == "recorded_fixture" for case in cases))
        self.assertTrue(all(case["source_payload"].get("order_id") for case in cases))
        self.assertEqual("CLOSED", database.get_case_by_scenario("refund-completed", self.db_path)["status"])

    def test_recorded_evidence_reevaluation_needs_no_external_api(self):
        demo.ensure_showcase(path=self.db_path)
        return_case = database.get_case_by_scenario("doa-evidence", self.db_path)
        updated = demo.reevaluate_recorded_case(
            return_case, evidence_received=True, path=self.db_path
        )
        self.assertEqual("WAITING_HUMAN_APPROVAL", updated["status"])
        self.assertEqual("procedi_swap", updated["suggested_resolution"])

    def test_reset_removes_only_portfolio_cases(self):
        demo.ensure_showcase(path=self.db_path)
        database.create_case(
            {
                "id": "MANUAL-CASE",
                "request_date": database.utc_now(),
                "return_type": "escalation",
                "return_reason": "altro",
                "customer_message": "Pratica manuale",
                "ai_classification": {},
                "eligibility_result": "manual_review",
                "suggested_resolution": "escalation_operatore",
            },
            path=self.db_path,
        )
        reset_cases = demo.reset_showcase(path=self.db_path)
        self.assertEqual(6, len(reset_cases))
        self.assertIsNotNone(database.get_case("MANUAL-CASE", self.db_path))


if __name__ == "__main__":
    unittest.main()
