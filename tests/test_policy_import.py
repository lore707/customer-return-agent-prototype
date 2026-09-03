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


if __name__ == "__main__":
    unittest.main()
