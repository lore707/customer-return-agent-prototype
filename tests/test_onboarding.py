import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import app
import context_privacy
import database


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_database = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "onboarding.db")
        database.init_database()
        app.app.config.update(TESTING=True)
        self.client = app.app.test_client()

    def tearDown(self):
        if self.previous_database is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.previous_database
        self.temp_dir.cleanup()

    def _configure_to_model(self):
        started = self.client.post("/api/onboarding/start")
        self.assertEqual(200, started.status_code)
        company = self.client.post(
            "/api/onboarding/company",
            json={
                "company_name": "Northstar Services",
                "company_description": "We coordinate complex supplier work for distributed client teams.",
                "industry": "Professional services",
                "markets": "Italy, France",
                "business_model": "B2B",
                "team_size": "11–50",
            },
        )
        self.assertEqual(200, company.status_code)
        operation = self.client.post(
            "/api/onboarding/operation",
            json={
                "description": "Coordinate vendor onboarding requests and assign every accepted request to an accountable owner.",
                "objective": "Reach a safe and traceable decision within one business day.",
                "current_process": "Requests arrive by email. The coordinator checks the deadline and required documents before assigning work.",
            },
        )
        self.assertEqual(200, operation.status_code)
        operation_id = operation.get_json()["operation"]["id"]
        knowledge = self.client.post(
            "/api/onboarding/knowledge",
            data={
                "pasted_text": "Every request must include a business reason and deadline. Before work starts, the process owner must approve exceptions. Urgent requests require escalation to the manager."
            },
        )
        self.assertEqual(200, knowledge.status_code)
        analyzed = self.client.post("/api/onboarding/analyze")
        self.assertEqual(200, analyzed.status_code)
        payload = analyzed.get_json()
        self.assertEqual("1.0", payload["model"]["schema_version"])
        self.assertGreaterEqual(len(payload["model"]["rules"]), 2)
        return operation_id, payload

    def test_end_to_end_onboarding_configures_the_main_workspace(self):
        operation_id, payload = self._configure_to_model()
        if payload["clarifications"]:
            answers = {item["id"]: item["options"][0] for item in payload["clarifications"]}
            clarified = self.client.post("/api/onboarding/clarifications", json={"answers": answers})
            self.assertEqual(200, clarified.status_code)

        tests = self.client.post("/api/onboarding/model-reviewed")
        self.assertEqual(200, tests.status_code)
        scenarios = tests.get_json()["scenarios"]
        self.assertEqual(3, len(scenarios))
        feedback = [{"id": item["id"], "status": "correct", "feedback": "Matches our process."} for item in scenarios]
        self.assertEqual(200, self.client.post("/api/onboarding/tests", json={"feedback": feedback}).status_code)
        completed = self.client.post("/api/onboarding/complete")
        self.assertEqual(200, completed.status_code)

        workbench = self.client.get("/workbench")
        self.assertEqual(200, workbench.status_code)
        self.assertIn(b"Coordinate vendor onboarding requests", workbench.data)

        created = self.client.post(
            "/api/workbench/analyze",
            json={
                "workflow": f"operation:{operation_id}",
                "message": "A supplier onboarding request is urgent and includes the required documents.",
            },
        )
        self.assertEqual(200, created.status_code)
        case_id = created.get_json()["case_id"]
        case = database.get_case(case_id)
        self.assertEqual(f"operation:{operation_id}", case["workflow_key"])
        self.assertEqual("policy_copilot_configured", case["source_mode"])

        playbooks = self.client.get("/playbooks")
        self.assertEqual(200, playbooks.status_code)
        self.assertIn(b"Coordinate vendor onboarding requests", playbooks.data)
        self.assertNotIn(b"tre workflow", playbooks.data)

    def test_privacy_layer_redacts_identifiers_and_secrets(self):
        context = context_privacy.prepare_operational_context(
            {
                "company_name": "Acme",
                "company_description": "Contact luca@example.com about the process.",
                "markets": ["Italy"],
            },
            {
                "description": "Handle internal access requests safely.",
                "objective": "Reduce manual routing.",
                "current_process": "api_key=super-secret should never leave the context layer",
            },
            [],
        )
        serialized = str(context)
        self.assertNotIn("luca@example.com", serialized)
        self.assertNotIn("super-secret", serialized)
        self.assertGreaterEqual(context["privacy"]["redactions"], 2)

    def test_document_upload_is_optional(self):
        self.client.post("/api/onboarding/start")
        self.client.post(
            "/api/onboarding/company",
            json={
                "company_name": "Acme",
                "company_description": "A small company coordinating recurring internal operations.",
                "industry": "Other", "markets": "Italy", "business_model": "B2B", "team_size": "1–10",
            },
        )
        self.client.post(
            "/api/onboarding/operation",
            json={
                "description": "Manage recurring operational requests from initial intake to owner assignment.",
                "objective": "Give every request a clear and accountable next action.",
                "current_process": "",
            },
        )
        self.assertEqual(200, self.client.post("/api/onboarding/knowledge", data={}).status_code)
        analyzed = self.client.post("/api/onboarding/analyze")
        self.assertEqual(200, analyzed.status_code)
        self.assertEqual(0, analyzed.get_json()["model"]["knowledge"]["source_count"])


if __name__ == "__main__":
    unittest.main()
