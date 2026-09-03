import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import app
import database


def analyzed_result(outcome="procedi_rimborso"):
    return {
        "session_id": "session-app-test",
        "stato": {"categoria": "recesso", "numero_ordine": "1002"},
        "tipo": "pacchetto",
        "pacchetto": {
            "contesto": {
                "numero_ordine": "1002",
                "categoria": "recesso",
                "data_consegna": "2026-07-17",
                "regola_applicata": "Recesso entro 14 giorni",
                "confidence": 0.96,
                "ordine": {
                    "shopify_order_id": 22,
                    "numero_ordine": "#1002",
                    "customer_id": 7,
                    "customer_name": "Mario Rossi",
                    "email_cliente": "mario@example.test",
                    "data_acquisto": "2026-07-15T10:00:00Z",
                    "prodotti": [
                        {
                            "line_item_id": 3,
                            "titolo": "Asciugacapelli",
                            "sku": "HAIR-1",
                            "variant_id": 4,
                            "quantita": 1,
                        }
                    ],
                },
            },
            "azione_proposta": {
                "esito_proposto": outcome,
                "prossima_azione": "Generare etichetta mock",
            },
            "policy_evaluation": {
                "rule_id": "withdrawal_eligible",
                "policy_sections": ["§1", "§4", "§6"],
                "policy_version": "2026-09-02",
            },
            "bozza_risposta": "La richiesta è stata accettata.",
        },
    }


class AppWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_database = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self.temp_dir.name) / "app.db")
        database.init_database()
        app.app.config.update(TESTING=True)
        self.client = app.app.test_client()

    def tearDown(self):
        if self.previous_database is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self.previous_database
        self.temp_dir.cleanup()

    def test_full_mock_refund_workflow(self):
        with (
            patch.object(app.conversation, "processa_messaggio", return_value=analyzed_result()),
            patch.object(
                app.conversation,
                "trascrizione_cliente",
                return_value="Ordine 1002, vorrei fare un reso",
            ),
        ):
            response = self.client.post(
                "/messaggio",
                json={"session_id": "session-app-test", "messaggio": "Reso 1002"},
            )
        self.assertEqual(200, response.status_code)
        case_id = response.get_json()["case_id"]
        self.assertEqual("WAITING_HUMAN_APPROVAL", database.get_case(case_id)["status"])
        detail = self.client.get(f"/cases/{case_id}")
        self.assertEqual(200, detail.status_code)
        self.assertIn(b"Return Operations", detail.data)

        approval = self.client.post(
            "/conferma",
            json={
                "case_id": case_id,
                "azione": "accetta",
                "testo_finale": "Risposta approvata e modificata.",
                "numero_ordine": "TENTATIVO-DI-MODIFICA",
            },
        )
        self.assertEqual(200, approval.status_code)
        approved = database.get_case(case_id)
        self.assertEqual("WAITING_FOR_RETURN", approved["status"])
        self.assertEqual("1002", approved["shopify_order_number"])
        self.assertEqual("mock", approved["shipping_provider"])
        self.assertTrue(approved["tracking_number"].startswith("MOCK"))
        self.assertEqual("modified_and_approved", approved["human_decision"])

        response = self.client.post(
            f"/cases/{case_id}/advance", json={"azione": "received"}
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("RETURN_RECEIVED", database.get_case(case_id)["status"])

        # L'arrivo del pacco non autorizza da solo il rimborso: serve il
        # controllo fisico esplicito dell'operatore.
        blocked = self.client.post(
            f"/cases/{case_id}/advance", json={"azione": "start_resolution"}
        )
        self.assertEqual(409, blocked.status_code)

        incomplete = self.client.post(
            f"/cases/{case_id}/advance",
            json={"azione": "validate_return", "checks": {"product_condition": True}},
        )
        self.assertEqual(409, incomplete.status_code)

        steps = [
            ("validate_return", "RETURN_VALIDATED", {
                "product_condition": True,
                "serial_and_accessories": True,
                "product_packaging": True,
            }),
            ("start_resolution", "REFUND_PENDING", None),
            ("complete", "CLOSED", None),
        ]
        for action, expected, checks in steps:
            payload = {"azione": action}
            if checks:
                payload["checks"] = checks
            response = self.client.post(
                f"/cases/{case_id}/advance", json=payload
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual(expected, database.get_case(case_id)["status"])

        timeline = database.get_timeline(case_id)
        self.assertGreaterEqual(len(timeline), 10)
        dashboard = self.client.get("/")
        self.assertEqual(200, dashboard.status_code)
        self.assertIn(case_id.encode(), dashboard.data)
        register = self.client.get("/database")
        self.assertEqual(200, register.status_code)
        self.assertIn(b"Database resi", register.data)
        self.assertGreaterEqual(len(database.get_case_messages(case_id)), 2)

    def test_confirmation_requires_server_side_case(self):
        response = self.client.post(
            "/conferma", json={"case_id": "missing", "azione": "accetta"}
        )
        self.assertEqual(404, response.status_code)

    def test_followup_is_saved_on_the_same_case(self):
        with (
            patch.object(app.conversation, "processa_messaggio", return_value=analyzed_result()),
            patch.object(
                app.conversation,
                "trascrizione_cliente",
                return_value="Ordine 1002, vorrei fare un reso",
            ),
        ):
            first = self.client.post(
                "/messaggio",
                json={"session_id": "session-app-test", "messaggio": "Reso 1002"},
            )
        case_id = first.get_json()["case_id"]

        with patch.object(
            app.conversation, "processa_messaggio", return_value=analyzed_result()
        ):
            followup = self.client.post(
                "/messaggio",
                json={
                    "case_id": case_id,
                    "session_id": "session-app-test",
                    "messaggio": "Confermo che il prodotto non ha difetti.",
                },
            )
        self.assertEqual(200, followup.status_code)
        self.assertEqual(case_id, followup.get_json()["case_id"])
        self.assertEqual(1, database.analytics()["total"])
        customer_messages = [
            item
            for item in database.get_case_messages(case_id)
            if item["role"] == "cliente"
        ]
        self.assertEqual(2, len(customer_messages))

    def test_input_length_is_limited(self):
        response = self.client.post(
            "/messaggio",
            json={"session_id": "x", "messaggio": "a" * 5001},
        )
        self.assertEqual(400, response.status_code)

    def test_evidence_category_and_no_stock_fallback_follow_policy(self):
        app.demo.ensure_showcase()
        return_case = database.get_case_by_scenario("doa-evidence")
        evidence = self.client.post(
            f"/cases/{return_case['id']}/evidence",
            json={"evidence_category": "doa"},
        )
        self.assertEqual(200, evidence.status_code)
        updated = database.get_case(return_case["id"])
        self.assertEqual("doa_warranty_swap", updated["policy_decision"]["rule_id"])

        approval = self.client.post(
            "/conferma",
            json={"case_id": return_case["id"], "azione": "accetta"},
        )
        self.assertEqual(200, approval.status_code)
        self.client.post(
            f"/cases/{return_case['id']}/advance", json={"azione": "received"}
        )
        checks = {
            "product_condition": True,
            "serial_and_accessories": True,
            "product_packaging": True,
        }
        valid = self.client.post(
            f"/cases/{return_case['id']}/advance",
            json={"azione": "validate_return", "checks": checks},
        )
        self.assertEqual(200, valid.status_code)
        fallback = self.client.post(
            f"/cases/{return_case['id']}/advance",
            json={"azione": "start_refund_no_stock"},
        )
        self.assertEqual(200, fallback.status_code)
        updated = database.get_case(return_case["id"])
        self.assertEqual("REFUND_PENDING", updated["status"])
        self.assertEqual("unavailable", updated["replacement_status"])

    def test_separate_portfolio_sections_render(self):
        app.demo.ensure_showcase()
        expectations = {
            "/": b"I resi preparati",
            "/dashboard": b"Dashboard resi",
            "/workbench": b"AI draft response",
            "/policies": b"Flusso decisionale",
            "/database": b"Database resi",
            "/analytics": b"Performance agente",
        }
        for url, marker in expectations.items():
            with self.subTest(url=url):
                response = self.client.get(url, follow_redirects=True)
                self.assertEqual(200, response.status_code)
                self.assertIn(marker, response.data)

    def test_workbench_demo_feedback_and_followup_are_persisted(self):
        app.demo.ensure_showcase()
        approval_case = database.get_case_by_scenario("withdrawal-approved")
        original = approval_case["original_suggested_response"]
        regenerated = self.client.post(
            f"/cases/{approval_case['id']}/draft-action",
            json={
                "action": "regenerate",
                "reason_tag": "too_verbose",
                "instructions": "Rendi la risposta piu breve.",
            },
        )
        self.assertEqual(200, regenerated.status_code)
        self.assertNotEqual(
            original,
            database.get_case(approval_case["id"])["original_suggested_response"],
        )
        self.assertEqual(
            "too_verbose",
            database.get_case_feedback(approval_case["id"])[0]["reason_tag"],
        )

        evidence_case = database.get_case_by_scenario("doa-evidence")
        followup = self.client.post(
            f"/cases/{evidence_case['id']}/simulate-message",
            json={"message": "Allego foto e video del difetto con il seriale."},
        )
        self.assertEqual(200, followup.status_code)
        self.assertEqual(
            "WAITING_HUMAN_APPROVAL",
            database.get_case(evidence_case["id"])["status"],
        )
        self.assertTrue(
            any(
                item["message_type"] == "customer_followup"
                for item in database.get_case_messages(evidence_case["id"])
            )
        )


if __name__ == "__main__":
    unittest.main()
