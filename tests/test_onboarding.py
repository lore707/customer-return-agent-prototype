import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import app
import context_privacy
import database
import onboarding_store


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_database = os.environ.get("DATABASE_PATH")
        self.previous_provider = os.environ.get("OPERATIONAL_MODEL_PROVIDER")
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "onboarding.db")
        os.environ["OPERATIONAL_MODEL_PROVIDER"] = "local"
        database.init_database()
        app.app.config.update(TESTING=True)
        self.client = app.app.test_client()

    def _analyze_operation(self):
        started = self.client.post("/api/onboarding/analyze")
        self.assertEqual(202, started.status_code, started.get_data(as_text=True))
        self.assertEqual("processing", started.get_json()["status"])
        for _ in range(300):
            status = self.client.get("/api/onboarding/analyze/status")
            if status.status_code == 200:
                payload = status.get_json()
                if payload.get("status") == "complete":
                    return payload
            elif status.status_code >= 400:
                self.fail(status.get_data(as_text=True))
            time.sleep(0.01)
        self.fail("Operational model generation did not complete in time.")

    def tearDown(self):
        if self.previous_database is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.previous_database
        if self.previous_provider is None:
            os.environ.pop("OPERATIONAL_MODEL_PROVIDER", None)
        else:
            os.environ["OPERATIONAL_MODEL_PROVIDER"] = self.previous_provider
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
        payload = self._analyze_operation()
        self.assertEqual("1.2", payload["model"]["schema_version"])
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
        reviewed = self.client.post("/api/onboarding/tests", json={"feedback": feedback})
        self.assertEqual(200, reviewed.status_code)
        reviewed_model = reviewed.get_json()["model"]
        self.assertEqual(len(scenarios), reviewed_model["validation"]["passed"])
        self.assertGreater(reviewed_model["completeness"], payload["model"]["completeness"])
        completed = self.client.post("/api/onboarding/complete")
        self.assertEqual(200, completed.status_code)

        self.assertEqual('/memory', completed.get_json()['redirect'])
        workbench = self.client.get("/workbench", follow_redirects=True)
        self.assertEqual(200, workbench.status_code)
        self.assertIn(b"vendor onboarding requests", workbench.data)

        created = self.client.post(
            "/api/workbench/analyze",
            json={
                "workflow": f"operation:{operation_id}",
                "message": "A supplier onboarding request is urgent and includes the required documents.",
            },
        )
        # An onboarding draft must not enter the legacy first-rule evaluator.
        self.assertEqual(409, created.status_code)
        memory = self.client.get('/api/memory').get_json()
        self.assertEqual(0, memory['published_version'])
        self.assertTrue(all(p['status']=='draft' for p in memory['draft']['processes']))

        playbooks = self.client.get("/playbooks")
        self.assertEqual(200, playbooks.status_code)
        self.assertIn(b"vendor onboarding requests", playbooks.data)
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
        analyzed = self._analyze_operation()
        self.assertEqual(0, analyzed["model"]["knowledge"]["source_count"])

    def test_simplified_onboarding_builds_company_specific_operational_memory(self):
        started = self.client.post("/api/onboarding/start")
        self.assertEqual(200, started.status_code)
        company = self.client.post(
            "/api/onboarding/company",
            json={
                "company_name": "MangoPeach",
                "industry": "Professional services",
                "markets": "Italia, Europa",
                "team_size": "11–50",
            },
        )
        self.assertEqual(200, company.status_code, company.get_data(as_text=True))
        operation = self.client.post(
            "/api/onboarding/operation",
            json={
                "core_business": "Aiutiamo aziende a ridisegnare processi operativi complessi con software, automazione e supervisione umana.",
                "operational_activities": "Raccogliamo requisiti, ricostruiamo il processo attuale, progettiamo il processo futuro, testiamo la soluzione e documentiamo gli esiti.",
                "operational_challenges": "Le procedure dei clienti sono spesso incomplete, contraddittorie e distribuite tra documenti e persone diverse.",
            },
        )
        self.assertEqual(200, operation.status_code, operation.get_data(as_text=True))
        state = onboarding_store.onboarding_state(started.get_json()["workspace"]["id"])
        self.assertEqual(
            "Le procedure dei clienti sono spesso incomplete, contraddittorie e distribuite tra documenti e persone diverse.",
            state["workspace"]["derived_context"]["operational_challenges"],
        )
        self.assertEqual(200, self.client.post("/api/onboarding/knowledge", data={}).status_code)
        analyzed = self._analyze_operation()
        self.assertTrue(analyzed["model"]["operational_domains"])
        self.assertNotIn("Standard request", {item["name"] for item in analyzed["model"]["case_types"]})

    def test_onboarding_page_exposes_the_six_step_operational_memory_flow(self):
        response = self.client.get("/onboarding")
        self.assertEqual(200, response.status_code)
        body = response.get_data(as_text=True)
        self.assertIn("Racconta la tua azienda", body)
        self.assertIn("Qual è il core business", body)
        self.assertIn("Costruisci il mio Ops", body)
        self.assertIn("MEMORIA OPERATIVA AZIENDALE", body)
        self.assertNotIn("scenarioForm", body)

    def test_manager_can_edit_and_persist_the_generated_operational_document(self):
        operation_id, payload = self._configure_to_model()
        model = payload["model"]
        response = self.client.post(
            "/api/onboarding/model",
            json={
                "operation": {"name": "Vendor intake", "purpose": "Qualify and assign every supplier request to an accountable owner."},
                "case_types": [{"name": "New vendor"}, {"name": "Urgent exception"}],
                "required_fields": [{"label": "Business reason"}, {"label": "Deadline"}],
                "process_steps": [{"actor": "Coordinator", "action": "Checks the request", "result": "Request qualified"}],
                "rules": [{"id": "VEN-01", "statement": "The request is complete", "action": "Assign the accountable owner"}],
                "escalations": [{"owner": "Operations Manager", "trigger": "The request is outside the playbook"}],
            },
        )
        self.assertEqual(200, response.status_code)
        reviewed = response.get_json()["model"]
        self.assertEqual("Vendor intake", reviewed["operation"]["name"])
        self.assertEqual("human_review", reviewed["rules"][0]["origin"])
        stored = onboarding_store.get_operation(operation_id)
        self.assertEqual("Vendor intake", stored["name"])
        self.assertEqual("Vendor intake", stored["operational_model"]["operation"]["name"])


if __name__ == "__main__":
    unittest.main()
