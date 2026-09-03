import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import database
import domain


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        database.init_database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_case(self):
        return database.create_case(
            {
                "session_id": "session-test",
                "shopify_order_number": "1002",
                "product_name": "Asciugacapelli",
                "sku": "SKU-TEST",
                "request_date": database.utc_now(),
                "return_type": "right_of_withdrawal",
                "return_reason": "recesso",
                "customer_message": "Vorrei restituire il prodotto",
                "ai_classification": {"categoria": "recesso", "confidence": 0.95},
                "confidence": 0.95,
                "eligibility_result": "eligible",
                "suggested_resolution": "procedi_rimborso",
                "original_suggested_response": "Bozza",
            },
            self.db_path,
        )

    def test_case_survives_new_connection_and_has_timeline(self):
        return_case = self.create_case()
        database.transition_case(
            return_case["id"],
            domain.CaseStatus.ANALYZED.value,
            event_type="analyzed",
            path=self.db_path,
        )
        loaded = database.get_case(return_case["id"], self.db_path)
        events = database.get_timeline(return_case["id"], self.db_path)
        self.assertEqual(domain.CaseStatus.ANALYZED.value, loaded["status"])
        self.assertEqual(2, len(events))

    def test_invalid_transition_is_rejected(self):
        return_case = self.create_case()
        with self.assertRaises(domain.InvalidTransition):
            database.transition_case(
                return_case["id"],
                domain.CaseStatus.REFUNDED.value,
                event_type="invalid",
                path=self.db_path,
            )

    def test_search_and_analytics(self):
        self.create_case()
        self.assertEqual(1, len(database.list_cases(query="SKU-TEST", path=self.db_path)))
        metrics = database.analytics(self.db_path)
        self.assertEqual(1, metrics["total"])
        self.assertEqual(1, metrics["open"])

    def test_conversation_messages_are_linked_to_case(self):
        database.add_message(
            "session-test",
            "cliente",
            "Prima e-mail del cliente",
            path=self.db_path,
        )
        return_case = self.create_case()
        database.link_session_messages(
            "session-test", return_case["id"], path=self.db_path
        )
        database.add_message(
            "session-test",
            "agente",
            "Risposta dell'agente",
            case_id=return_case["id"],
            path=self.db_path,
        )
        messages = database.get_case_messages(return_case["id"], self.db_path)
        self.assertEqual(["cliente", "agente"], [item["role"] for item in messages])
        self.assertEqual(2, database.message_counts([return_case["id"]], self.db_path)[return_case["id"]])

    def test_policy_decision_is_persisted_as_structured_data(self):
        return_case = database.create_case(
            {
                "request_date": database.utc_now(),
                "return_type": "right_of_withdrawal",
                "return_reason": "recesso",
                "customer_message": "Reso",
                "ai_classification": {},
                "eligibility_result": "eligible",
                "policy_decision": {"rule_id": "withdrawal_eligible"},
                "suggested_resolution": "procedi_rimborso",
            },
            self.db_path,
        )
        self.assertEqual("withdrawal_eligible", return_case["policy_decision"]["rule_id"])

    def test_policy_timeouts_close_missing_evidence_and_unshipped_return(self):
        evidence_case = database.create_case(
            {
                "request_date": database.utc_now(), "return_type": "defective_product",
                "return_reason": "doa", "customer_message": "Difetto",
                "ai_classification": {}, "eligibility_result": "needs_information",
                "suggested_resolution": "chiedi_foto_video",
            }, self.db_path,
        )
        database.transition_case(evidence_case["id"], "ANALYZED", event_type="analyzed", path=self.db_path)
        database.transition_case(evidence_case["id"], "NEEDS_INFORMATION", event_type="evidence_requested", path=self.db_path)

        shipping_case = self.create_case()
        for target, event in [
            ("ANALYZED", "analyzed"), ("WAITING_HUMAN_APPROVAL", "policy_evaluated"),
            ("APPROVED", "return_approved"), ("LABEL_CREATED", "label_created"),
            ("WAITING_FOR_RETURN", "waiting_for_customer_return"),
        ]:
            database.transition_case(shipping_case["id"], target, event_type=event, path=self.db_path)
        database.update_case(
            shipping_case["id"], {"label_status": "created"},
            event_type="return_label_generated", path=self.db_path,
        )
        with database.session(self.db_path) as conn:
            conn.execute("UPDATE return_cases SET created_at = '2026-08-01T00:00:00+00:00'")
            conn.execute(
                "UPDATE audit_events SET created_at = '2026-08-01T00:00:00+00:00' "
                "WHERE event_type = 'return_label_generated'"
            )

        result = database.apply_policy_timeouts(
            no_response_days=15, unshipped_days=15,
            now=datetime(2026, 9, 2, tzinfo=timezone.utc), path=self.db_path,
        )
        self.assertEqual({"closed_no_response": 1, "closed_unshipped": 1}, result)
        self.assertEqual("CLOSED", database.get_case(evidence_case["id"], self.db_path)["status"])
        expired = database.get_case(shipping_case["id"], self.db_path)
        self.assertEqual("CLOSED", expired["status"])
        self.assertEqual("cancelled", expired["label_status"])


if __name__ == "__main__":
    unittest.main()
