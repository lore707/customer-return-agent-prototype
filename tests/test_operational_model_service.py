import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from operational_model_service import LocalOperationalModelService


def context_for(operation: str, knowledge: str = "", *, industry: str = "Operations") -> dict:
    sources = []
    if knowledge:
        sources.append(
            {
                "id": "SRC-01",
                "name": "Operating notes",
                "source_type": "text",
                "content": knowledge,
            }
        )
    return {
        "company": {
            "name": "Example Company",
            "description": "A team coordinating recurring operational work.",
            "industry": industry,
            "markets": ["Italy"],
            "business_model": "B2B",
            "team_size": "11-50",
        },
        "operation": {
            "name": "",
            "description": operation,
            "objective": "Make every recommendation consistent, traceable and ready for human review.",
            "current_process": "Requests arrive by email and an operator applies the documented rules.",
        },
        "knowledge_sources": sources,
        "privacy": {"redactions": 0, "external_provider_used": False},
    }


class OperationalModelSemanticTests(unittest.TestCase):
    def setUp(self):
        self.service = LocalOperationalModelService()

    def test_returns_knowledge_becomes_a_domain_specific_model(self):
        knowledge = """
        Il cliente comunica il numero ordine, la data di consegna e il motivo della richiesta.
        Distinguiamo diritto di recesso, prodotto difettoso / DOA e richiesta in garanzia.
        Il recesso è consentito entro 14 giorni dalla consegna. Il costo ritorno è €7,90.
        Per un DOA sono obbligatori il video del difetto e il numero seriale.
        Se le prove sono mancanti, chiedere informazioni e mantenere aperto il caso.
        La garanzia copre i difetti entro 24 mesi dalla consegna.
        Se il difetto è confermato, procedere con lo swap.
        Il database conserva lo stato del rientro del prodotto.
        Un caso non coperto o ambiguo deve essere escalato al Manager.
        """
        model = self.service.build(
            context_for("Gestire resi, DOA e garanzie dall'apertura alla risoluzione.", knowledge, industry="E-commerce")
        )["model"]

        self.assertIn("resi", model["operation"]["name"].casefold())
        case_text = " | ".join(item["name"] for item in model["case_types"]).casefold()
        self.assertIn("diritto di recesso", case_text)
        self.assertIn("prodotto difettoso", case_text)
        self.assertIn("richiesta in garanzia", case_text)
        self.assertIn("non coperto", case_text)
        self.assertNotIn("standard request", case_text)

        field_text = " | ".join(item["label"] for item in model["required_fields"]).casefold()
        for expected in ("numero ordine", "data di consegna", "motivo della richiesta", "video del difetto", "numero seriale", "stato del rientro"):
            self.assertIn(expected, field_text)
        rule_text = " | ".join(item["statement"] for item in model["rules"]).casefold()
        for expected in ("14 giorni", "€7,90", "video del difetto", "numero seriale", "24 mesi", "swap", "manager"):
            self.assertIn(expected, rule_text)
        self.assertIn("manager", model["escalations"][0]["owner"].casefold())
        self.assertLessEqual(model["completeness"], 88)
        self.assertEqual(0, model["completeness_scores"]["scenario_validation"])

    def test_sparse_generic_input_cannot_claim_high_completeness(self):
        model = self.service.build(
            context_for("Gestire meglio le richieste operative ricorrenti del team.")
        )["model"]
        self.assertLess(model["completeness"], 60)
        self.assertEqual("unclassified_operation", model["case_types"][0]["id"])
        self.assertGreater(len(model["ambiguities"]), 0)

    def test_other_domains_are_discovered_without_a_preconfigured_workflow(self):
        knowledge = """
        Case types: purchase request, supplier / vendor onboarding and budget exception.
        Ogni purchase request deve includere business reason, deadline, supplier e budget disponibile.
        Il supplier / vendor onboarding richiede la conferma del process owner.
        Una budget exception deve essere approvata dal Manager prima dell'acquisto.
        """
        model = self.service.build(
            context_for("Coordinate vendor onboarding and purchase requests.", knowledge, industry="Professional services")
        )["model"]
        names = {item["name"] for item in model["case_types"]}
        self.assertIn("vendor onboarding", model["operation"]["name"].casefold())
        lowered_names = {item.casefold() for item in names}
        self.assertTrue({"purchase request", "supplier / vendor onboarding", "budget exception"}.issubset(lowered_names))
        self.assertNotIn("diritto di recesso", lowered_names)

    def test_human_scenario_review_changes_the_evidence_score(self):
        model = self.service.build(
            context_for(
                "Gestire diritto di recesso e prodotto difettoso / DOA.",
                "Recesso entro 14 giorni dalla data di consegna. Per il DOA servono video del difetto e seriale. I casi ambigui vanno al Manager.",
                industry="E-commerce",
            )
        )["model"]
        scenarios = self.service.scenarios(model)
        reviewed = [{**item, "status": "correct"} for item in scenarios]
        updated = self.service.apply_test_feedback(model, reviewed)
        self.assertGreater(updated["completeness"], model["completeness"])
        self.assertEqual(len(scenarios), updated["validation"]["reviewed"])
        self.assertEqual(10, updated["completeness_scores"]["scenario_validation"])

    def test_messy_notes_become_a_complete_reviewable_document_without_external_ai(self):
        knowledge = """
        Il cliente segnala il problema. L'operatore chiede numero ordine o email di conferma.
        Distinguiamo diritto di recesso, prodotto difettoso / DOA e richiesta in garanzia.
        L'operatore aggiorna il database con numero ordine, stato, prodotto e problematica.
        Per il DOA sono obbligatori video e seriale. Se il DOA è confermato, crea uno swap.
        Per il diritto di recesso entro 14 giorni, procede con il rimborso.
        I casi ambigui vanno escalati al Manager. Crea l'etichetta su Sendcloud,
        carica il tracking su Shopify e, se l'automazione non parte, la avvia su Make.
        """
        model = self.service.build(
            context_for("Gestire resi, recesso, DOA e garanzia dall'apertura alla chiusura.", knowledge, industry="E-commerce")
        )["model"]
        self.assertEqual("local_evidence_extractor", model["provider"])
        self.assertEqual("1.2", model["schema_version"])
        self.assertGreaterEqual(len(model["process"]["steps"]), 5)
        self.assertTrue({"shopify", "sendcloud", "make"}.issubset(set(model["process"]["systems"])))
        self.assertIn("operator", " ".join(model["process"]["roles"]))
        outcome_text = " ".join(item["description"] for item in model["outcomes"]).casefold()
        self.assertIn("swap", outcome_text)
        self.assertIn("rimborso", outcome_text)
        self.assertTrue(all(item["origin"] == "contextual_suggestion" for item in model["assumptions"]))

    def test_manager_review_replaces_generated_content_with_verified_content(self):
        model = self.service.build(
            context_for("Gestire richieste di acquisto e relative eccezioni.", "Tipi di richiesta: acquisto standard, eccezione budget. Ogni richiesta deve includere costo e motivazione. Le eccezioni vanno al Manager.")
        )["model"]
        reviewed = self.service.review(
            model,
            {
                "operation": {"name": "Purchase approvals", "purpose": "Route every purchase through the documented approval path."},
                "case_types": [{"name": "Standard purchase"}, {"name": "Budget exception"}],
                "required_fields": [{"label": "Cost"}, {"label": "Business reason"}],
                "rules": [{"id": "PUR-01", "statement": "Cost is below EUR 500", "action": "Send to the manager"}],
                "escalations": [{"owner": "Finance Manager", "trigger": "Budget is not assigned"}],
                "process_steps": [{"actor": "Requester", "action": "Submits the purchase request", "result": "Request recorded"}],
            },
        )
        self.assertEqual("human_reviewed", reviewed["playbook"]["status"])
        self.assertEqual("human_review", reviewed["rules"][0]["origin"])
        self.assertTrue(reviewed["review"]["model_edited"])


if __name__ == "__main__":
    unittest.main()
