import sys
import unittest
from io import BytesIO
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import policy_import


class PolicyImportTests(unittest.TestCase):
    def test_text_document_is_read_and_structured(self):
        text = policy_import.document_text(
            "policy.md",
            (
                "Il recesso è ammesso entro 14 giorni dalla data di consegna. "
                "Il prodotto deve essere integro e rivendibile. "
                "Per DOA servono foto e video. "
                "[DA CONFERMARE] Chi sostiene il costo del reso?"
            ).encode("utf-8"),
        )
        result = policy_import.extract_structured_rules(text)

        window = next(item for item in result["rules"] if item["id"] == "return_window")
        self.assertEqual("14 giorni", window["value"])
        self.assertEqual(1, result["confirmation_count"])

    def test_unsupported_document_is_rejected(self):
        with self.assertRaises(policy_import.PolicyImportError):
            policy_import.document_text("policy.exe", b"x" * 100)

    def test_docx_document_is_read(self):
        document = Document()
        document.add_paragraph(
            "Il recesso è ammesso entro 30 giorni dalla consegna e richiede "
            "un prodotto integro e rivendibile."
        )
        payload = BytesIO()
        document.save(payload)

        text = policy_import.document_text(
            "returns.docx",
            payload.getvalue(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        result = policy_import.extract_structured_rules(text)
        window = next(item for item in result["rules"] if item["id"] == "return_window")
        self.assertEqual("30 giorni", window["value"])

    def test_two_year_warranty_is_structured_separately(self):
        result = policy_import.extract_structured_rules(
            "Il recesso è ammesso entro 14 giorni dalla consegna. "
            "Un prodotto difettoso è coperto dalla garanzia per 2 anni. "
            "Foto e video sono obbligatori prima dello swap."
        )
        warranty = next(
            item for item in result["rules"] if item["id"] == "warranty_window"
        )
        self.assertEqual("2 anni (730 giorni)", warranty["value"])

    def test_agency_notes_become_a_generic_playbook(self):
        result = policy_import.extract_structured_rules(
            "Ogni nuovo progetto richiede un brief con obiettivo, scope e deliverable. "
            "Il budget deve essere approvato prima del kickoff. Un responsabile segue "
            "la delivery e ogni modifica deve essere valutata per impatto e costi."
        )
        self.assertEqual("agency", result["playbook_type"])
        self.assertEqual("intake_scope", result["rules"][0]["id"])
        self.assertTrue(result["normalized_document"])


if __name__ == "__main__":
    unittest.main()
