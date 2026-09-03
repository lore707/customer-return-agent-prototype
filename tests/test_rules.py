import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import rules


class RuleEngineTests(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 7, 21)
        self.delivery = {"tracking": "TEST", "delivered_at": "2026-07-17"}

    @staticmethod
    def order(product="Asciugacapelli", status="fulfilled"):
        return {
            "stato_evasione": status,
            "prodotti": [{"titolo": product, "quantita": 1, "prezzo": "100"}],
        }

    def decide(self, category, **kwargs):
        return rules.applica_regole(
            category,
            "1001",
            kwargs.pop("order", self.order()),
            kwargs.pop("delivery", self.delivery),
            oggi=self.today,
            **kwargs,
        )

    def test_standard_withdrawal_is_eligible(self):
        result = self.decide("recesso", confidence=0.95)
        self.assertEqual("procedi_rimborso", result["esito_proposto"])

    def test_hygiene_product_requires_seal_status(self):
        result = self.decide(
            "recesso", order=self.order("Rasoio elettrico"), confidence=0.95
        )
        self.assertEqual("chiedi_stato_sigillo", result["esito_proposto"])

    def test_sealed_hygiene_product_is_not_automatically_rejected(self):
        result = self.decide(
            "recesso",
            order=self.order("Rasoio elettrico"),
            sigillo_integro=True,
            confidence=0.95,
        )
        self.assertEqual("procedi_rimborso", result["esito_proposto"])

    def test_open_hygiene_product_is_rejected(self):
        result = self.decide(
            "recesso",
            order=self.order("Rasoio elettrico"),
            sigillo_integro=False,
            confidence=0.95,
        )
        self.assertEqual("rifiuta_recesso_prodotto_escluso", result["esito_proposto"])

    def test_requesting_evidence_is_not_receiving_evidence(self):
        missing = self.decide("doa", confidence=0.95, prove_fornite=False)
        received = self.decide("doa", confidence=0.95, prove_fornite=True)
        self.assertEqual("chiedi_foto_video", missing["esito_proposto"])
        self.assertEqual("offri_scelta_rimborso_o_swap", received["esito_proposto"])

    def test_low_confidence_escalates(self):
        result = self.decide("recesso", confidence=0.60)
        self.assertEqual("escalation_operatore", result["esito_proposto"])

    def test_medium_confidence_is_flagged_for_attention(self):
        result = self.decide("recesso", confidence=0.80)
        self.assertEqual("procedi_rimborso", result["esito_proposto"])
        self.assertEqual("attention", result["review_level"])

    def test_doa_respects_explicit_refund_choice(self):
        result = self.decide(
            "doa", confidence=0.95, prove_fornite=True,
            requested_resolution="refund",
        )
        self.assertEqual("procedi_rimborso", result["esito_proposto"])

    def test_doa_respects_explicit_swap_choice(self):
        result = self.decide(
            "doa", confidence=0.95, prove_fornite=True,
            requested_resolution="swap",
        )
        self.assertEqual("procedi_swap", result["esito_proposto"])

    def test_withdrawal_exposes_shipping_and_inspection_rules(self):
        result = self.decide("recesso", confidence=0.95)
        self.assertEqual("customer", result["shipping_payer"])
        self.assertTrue(result["deduct_shipping_from_refund"])
        self.assertTrue(result["physical_validation_required"])
        self.assertIn("external_packaging", result["customer_instructions"])

    def test_multiple_products_require_line_item_selection(self):
        order = self.order()
        order["prodotti"].append({"titolo": "Accessorio", "quantita": 1})
        result = self.decide("recesso", order=order, confidence=0.95)
        self.assertEqual("escalation_operatore", result["esito_proposto"])
        self.assertEqual("partial_return_line_item_required", result["rule_id"])

    def test_wrong_item_does_not_invent_unconfirmed_resolution(self):
        result = self.decide("articolo_errato", confidence=0.95)
        self.assertEqual("escalation_operatore", result["esito_proposto"])
        self.assertEqual("wrong_item_resolution", result["unresolved_policy"])

    def test_explicit_chargeback_signal_escalates(self):
        result = self.decide(
            "altro", confidence=0.98, escalation_reason="chargeback"
        )
        self.assertEqual("explicit_escalation_signal", result["rule_id"])

    def test_future_delivery_does_not_generate_negative_days(self):
        result = self.decide(
            "recesso",
            delivery={"tracking": "TEST", "delivered_at": "2026-08-01"},
            confidence=0.95,
        )
        self.assertIsNone(result["giorni_dalla_consegna"])
        self.assertEqual("escalation_operatore", result["esito_proposto"])


if __name__ == "__main__":
    unittest.main()
