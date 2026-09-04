import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import app
import database
import guided_demo


class GuidedDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_database = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "guided.db")
        database.init_database()
        app.app.config.update(TESTING=True)
        self.client = app.app.test_client()

    def tearDown(self):
        if self.previous_database is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.previous_database
        self.temp_dir.cleanup()

    def run_scenario(self, slug):
        response = self.client.post(f"/api/guided-demo/{slug}/start")
        self.assertEqual(200, response.status_code)
        state = response.get_json()
        while not state["completed"]:
            response = self.client.post(
                f"/api/guided-demo/cases/{state['case_id']}/next"
            )
            self.assertEqual(200, response.status_code, response.get_json())
            state = response.get_json()
        return state, database.get_case(state["case_id"])

    def test_doa_demo_runs_to_closed_swap(self):
        state, return_case = self.run_scenario("doa")
        self.assertEqual("CLOSED", return_case["status"])
        self.assertEqual("completed", return_case["replacement_status"])
        self.assertEqual("not_started", return_case["refund_status"])
        self.assertEqual("1051", return_case["replacement_order_number"])
        self.assertEqual("complete", return_case["integration_state"]["make"]["status"])
        self.assertEqual(5, return_case["customer_history"]["orders_total"])
        self.assertTrue(return_case["evidence"]["physical_inspection"]["declared_issue_confirmed"])
        self.assertGreaterEqual(len(database.get_timeline(return_case["id"])), 18)
        self.assertTrue(state["completed"])
        workbench = self.client.get(f"/workbench/{return_case['id']}")
        self.assertEqual(200, workbench.status_code)
        self.assertIn(b"Customer 360", workbench.data)
        self.assertIn(b"Ordine sostitutivo Shopify", workbench.data)
        self.assertIn(b"#1051", workbench.data)
        register = self.client.get("/database")
        self.assertEqual(200, register.status_code)
        self.assertIn(b"Scenario inviato alla logistica", register.data)

    def test_withdrawal_demo_runs_to_closed_refund(self):
        _, return_case = self.run_scenario("recesso")
        self.assertEqual("CLOSED", return_case["status"])
        self.assertEqual("completed", return_case["refund_status"])
        self.assertEqual("not_started", return_case["replacement_status"])
        self.assertEqual("not_required", return_case["integration_state"]["make"]["status"])
        self.assertEqual("withdrawal_eligible", return_case["policy_decision"]["rule_id"])

    def test_guided_pages_are_public_and_unknown_case_is_rejected(self):
        page = self.client.get("/demo/doa")
        self.assertEqual(200, page.status_code)
        self.assertIn("Dal problema alla sostituzione".encode(), page.data)
        missing = self.client.get("/demo/unknown")
        self.assertEqual(404, missing.status_code)
        invalid = self.client.post("/api/guided-demo/unknown/start")
        self.assertEqual(404, invalid.status_code)


if __name__ == "__main__":
    unittest.main()
