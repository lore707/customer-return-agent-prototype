import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import operational_grammar
from operational_model_service import AnthropicOperationalModelService


def element(kind, item_id, name, **changes):
    value = {
        "kind": kind,
        "id": item_id,
        "name": name,
        "summary": "Operational definition",
        "condition": "When the documented trigger occurs",
        "action": "Follow the documented action",
        "owner": "Process owner",
        "details": [],
        "links": [],
        "order": 0,
        "required": False,
        "source_type": "derived",
        "confidence": 0.8,
        "evidence": ["Supplied operating notes"],
        "requires_confirmation": True,
    }
    value.update(changes)
    return value


def payload():
    return {
        "schema_version": "2.0",
        "operation": {
            "name": "Client Process Redesign",
            "objective": "Deliver a validated and measurable target operating model.",
            "scope": "One client business process",
            "trigger": "A sponsor starts an engagement",
            "completion_definition": "The target process is launched and monitored",
            "source_type": "explicit",
            "confidence": 1.0,
            "evidence": ["Transform the client business process"],
            "requires_confirmation": False,
        },
        "elements": [
            element("case_type", "CASE-01", "Process redesign engagement"),
            element("actor", "ACT-01", "Consultant", details=["Map the current process"]),
            element("system", "SYS-01", "SOP repository"),
            element("input", "IN-01", "Existing procedures", required=True),
            element("stage", "STG-01", "Discovery", order=1, links=["IN-01"]),
            element("decision_rule", "RULE-01", "Validation gate", order=1, condition="The future process is ready", action="Obtain process owner validation"),
            element("escalation", "ESC-01", "Ownership conflict"),
            element("outcome", "OUT-01", "Validated target model"),
            element("metric", "MET-01", "Cycle time"),
        ],
    }


def context():
    return {
        "company": {
            "name": "Example Company",
            "description": "A consultancy improving internal operations.",
            "industry": "Professional services",
            "markets": ["Italy"],
            "business_model": "B2B",
            "team_size": "11-50",
        },
        "operation": {
            "name": "",
            "description": "Redesign a client business process.",
            "objective": "Deliver a validated and measurable target operating model.",
            "current_process": "Discover, map, design, validate and monitor.",
        },
        "knowledge_sources": [{"id": "SRC-01", "name": "Notes", "content": "The process owner validates the design."}],
        "privacy": {"redactions": 0, "external_provider_used": True},
    }


class OperationalGrammarTests(unittest.TestCase):
    def test_schema_uses_supported_compact_shape(self):
        value = json.dumps(operational_grammar.schema())
        for unsupported in ('"minimum"', '"maximum"', '"maxItems"'):
            self.assertNotIn(unsupported, value)
        self.assertEqual(["schema_version", "operation", "elements"], operational_grammar.schema()["required"])

    def test_compact_payload_validates_and_expands(self):
        value = payload()
        self.assertEqual([], operational_grammar.validate(value))
        expanded = operational_grammar.expand(value)
        self.assertEqual("Discovery", expanded["lifecycle"][0]["name"])
        self.assertEqual("Consultant", expanded["actors"][0]["name"])
        self.assertEqual("Cycle time", expanded["metrics"][0]["name"])

    def test_invalid_provenance_is_rejected(self):
        value = payload()
        value["elements"][0]["source_type"] = "suggested"
        value["elements"][0]["requires_confirmation"] = False
        self.assertTrue(any("must require confirmation" in item for item in operational_grammar.validate(value)))

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"})
    @patch("operational_model_service.anthropic.Anthropic")
    def test_anthropic_provider_requests_structured_output(self, anthropic_client):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload()))],
            usage=SimpleNamespace(input_tokens=321, output_tokens=654),
        )
        anthropic_client.return_value.messages.create.return_value = response
        model = AnthropicOperationalModelService().build(context())["model"]
        request = anthropic_client.return_value.messages.create.call_args.kwargs
        self.assertEqual("json_schema", request["output_config"]["format"]["type"])
        self.assertEqual("2.0", model["schema_version"])
        self.assertEqual(321, model["generation"]["input_tokens"])
        self.assertEqual("Discovery", model["process"]["steps"][0]["stage"])


if __name__ == "__main__":
    unittest.main()
