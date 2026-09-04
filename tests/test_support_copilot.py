import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import database
import support_copilot


class SupportCopilotTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "copilot.db"
        database.init_database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_message_is_anonymized_before_case_is_saved(self):
        case = support_copilot.create_case(
            "Sono Luca, luca@example.com, ordine #ABC123: il prodotto non funziona.",
            path=self.db_path,
        )
        self.assertEqual("doa", case["return_reason"])
        self.assertNotIn("luca@example.com", case["customer_message"])
        self.assertNotIn("ABC123", case["customer_message"])
        self.assertIn("[EMAIL]", case["customer_message"])
        self.assertEqual("NEEDS_INFORMATION", case["status"])

    def test_facts_produce_a_deterministic_decision_and_outcome(self):
        case = support_copilot.create_case(
            "Il dispositivo non si accende e vorrei usare la garanzia.",
            path=self.db_path,
        )
        for field, value in (
            ("purchase_verified", True),
            ("delivery_days", 80),
            ("evidence_received", True),
            ("serial_verified", True),
        ):
            case = support_copilot.update_fact(case["id"], field, value, path=self.db_path)
        self.assertEqual("WAITING_HUMAN_APPROVAL", case["status"])
        self.assertEqual("GAR-03", case["policy_decision"]["rule_id"])
        self.assertEqual("eligible", case["eligibility_result"])

        case = support_copilot.record_outcome(
            case["id"], "swap", case["original_suggested_response"], path=self.db_path
        )
        self.assertEqual("CLOSED", case["status"])
        self.assertEqual("swap", case["actual_outcome"])

    def test_demo_dataset_is_idempotent_and_drives_analytics(self):
        self.assertEqual(12, support_copilot.ensure_demo_cases(path=self.db_path))
        self.assertEqual(0, support_copilot.ensure_demo_cases(path=self.db_path))
        metrics = database.copilot_analytics(self.db_path)
        self.assertEqual(12, metrics["total"])
        self.assertEqual(12, metrics["sample_count"])
        self.assertEqual(2, metrics["escalated"])
        self.assertEqual(3, len(metrics["workflows"]))
        self.assertTrue(metrics["insights"])

    def test_agency_request_uses_its_own_facts_and_playbook(self):
        case = support_copilot.create_case(
            "Il cliente chiede una landing page per il lancio di ottobre.",
            workflow_key="agency_ops",
            path=self.db_path,
        )
        self.assertEqual("agency_project", case["return_reason"])
        self.assertEqual("agency_ops", case["workflow_key"])
        for field, value in (
            ("scope_clear", True),
            ("deadline_confirmed", True),
            ("budget_status", "approved"),
            ("owner_assigned", True),
        ):
            case = support_copilot.update_fact(case["id"], field, value, path=self.db_path)
        self.assertEqual("AGY-04", case["policy_decision"]["rule_id"])
        self.assertEqual("crea_brief", case["suggested_resolution"])
        view = support_copilot.view_model(case)
        self.assertEqual("Agenzia & delivery", view["workflow"]["label"])
        self.assertIn("brief_creato", view["outcome_labels"])
        self.assertNotIn("rimborso", view["outcome_labels"])

    def test_internal_incident_is_escalated_by_its_playbook(self):
        case = support_copilot.create_case(
            "Il sistema di reportistica è bloccato per tutto il team.",
            workflow_key="internal_ops",
            path=self.db_path,
        )
        for field, value in (
            ("urgency", "critical"),
            ("incident_impact", "business_blocked"),
            ("owner_assigned", False),
        ):
            case = support_copilot.update_fact(case["id"], field, value, path=self.db_path)
        self.assertEqual("INC-01", case["policy_decision"]["rule_id"])
        case = support_copilot.record_outcome(case["id"], "escalation", path=self.db_path)
        self.assertEqual("ESCALATED", case["status"])

    def test_policy_document_is_persisted_as_structured_rules(self):
        saved = database.publish_policy_document(
            "Policy test",
            [{"id": "RET-01", "label": "Finestra", "value": "14 giorni"}],
            confirmations=["Verificare eccezione"],
            normalized_document=[{"title": "Recesso", "items": ["14 giorni"]}],
            path=self.db_path,
        )
        loaded = database.get_policy_document(saved["id"], self.db_path)
        self.assertEqual("Policy test", loaded["name"])
        self.assertEqual("RET-01", loaded["rules"][0]["id"])
        self.assertEqual(1, len(database.list_policy_documents(self.db_path)))


if __name__ == "__main__":
    unittest.main()
