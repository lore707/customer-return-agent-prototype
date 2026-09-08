"""Versioned, domain-neutral grammar for reconstructing company operations.

The API transport is intentionally compact: one operation plus typed elements.
The application expands those elements into the richer ontology used by the UI.
This keeps Claude's constrained-output grammar small without hardcoding any
industry, company or workflow content.
"""

from __future__ import annotations

from copy import deepcopy


GRAMMAR_VERSION = "2.0"
SOURCE_TYPES = ("explicit", "derived", "suggested")
ELEMENT_KINDS = (
    "case_type", "actor", "system", "input", "stage", "decision_rule",
    "exception", "escalation", "constraint", "outcome", "metric",
    "feedback_loop", "ambiguity", "missing_knowledge",
)


def _object(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


# All fields are required to keep constrained-output grammar predictable and
# avoid optional-parameter state expansion. Empty strings/lists mean N/A.
OPERATIONAL_MODEL_SCHEMA = _object(
    {
        "schema_version": {"type": "string", "enum": [GRAMMAR_VERSION]},
        "operation": _object(
            {
                "name": {"type": "string"},
                "objective": {"type": "string"},
                "scope": {"type": "string"},
                "trigger": {"type": "string"},
                "completion_definition": {"type": "string"},
                "source_type": {"type": "string", "enum": list(SOURCE_TYPES)},
                "confidence": {"type": "number"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "requires_confirmation": {"type": "boolean"},
            },
            [
                "name", "objective", "scope", "trigger", "completion_definition",
                "source_type", "confidence", "evidence", "requires_confirmation",
            ],
        ),
        "elements": {
            "type": "array",
            "items": _object(
                {
                    "kind": {"type": "string", "enum": list(ELEMENT_KINDS)},
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "condition": {"type": "string"},
                    "action": {"type": "string"},
                    "owner": {"type": "string"},
                    "details": {"type": "array", "items": {"type": "string"}},
                    "links": {"type": "array", "items": {"type": "string"}},
                    "order": {"type": "integer"},
                    "required": {"type": "boolean"},
                    "source_type": {"type": "string", "enum": list(SOURCE_TYPES)},
                    "confidence": {"type": "number"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "requires_confirmation": {"type": "boolean"},
                },
                [
                    "kind", "id", "name", "summary", "condition", "action",
                    "owner", "details", "links", "order", "required",
                    "source_type", "confidence", "evidence", "requires_confirmation",
                ],
            ),
        },
    },
    ["schema_version", "operation", "elements"],
)


def schema() -> dict:
    """Return an isolated schema safe to pass to an external SDK."""
    return deepcopy(OPERATIONAL_MODEL_SCHEMA)


def _provenance(item: dict) -> dict:
    return {
        "source_type": item.get("source_type"),
        "confidence": item.get("confidence"),
        "evidence": item.get("evidence") or [],
        "requires_confirmation": bool(item.get("requires_confirmation")),
    }


def validate(payload: dict) -> list[str]:
    """Validate semantic invariants not guaranteed by JSON shape alone."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["The operational model is not an object."]
    if payload.get("schema_version") != GRAMMAR_VERSION:
        errors.append(f"schema_version must be {GRAMMAR_VERSION}.")
    operation = payload.get("operation") or {}
    if len(str(operation.get("name") or "").strip()) < 3:
        errors.append("The operation needs a specific name.")
    if len(str(operation.get("objective") or "").strip()) < 12:
        errors.append("The operation needs a meaningful objective.")

    def check_provenance(item: dict, location: str) -> None:
        if item.get("source_type") not in SOURCE_TYPES:
            errors.append(f"{location} has invalid provenance.")
        confidence = item.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
            errors.append(f"{location} confidence must be between 0 and 1.")
        if item.get("source_type") == "explicit" and not item.get("evidence"):
            errors.append(f"{location} is explicit but has no evidence.")
        if item.get("source_type") == "suggested" and not item.get("requires_confirmation"):
            errors.append(f"{location} is suggested and must require confirmation.")

    check_provenance(operation, "operation")
    elements = payload.get("elements")
    if not isinstance(elements, list):
        return [*errors, "elements must be a list."]
    seen_ids: set[str] = set()
    for index, item in enumerate(elements):
        if not isinstance(item, dict):
            errors.append(f"elements[{index}] must be an object.")
            continue
        if item.get("kind") not in ELEMENT_KINDS:
            errors.append(f"elements[{index}] has an invalid kind.")
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen_ids:
            errors.append(f"elements[{index}] needs a unique id.")
        seen_ids.add(item_id)
        check_provenance(item, f"elements[{index}]")
    stages = [item for item in elements if item.get("kind") == "stage"]
    orders = [item.get("order") for item in stages]
    if orders and (len(set(orders)) != len(orders) or sorted(orders) != list(range(1, len(orders) + 1))):
        errors.append("Lifecycle stage order must be unique and contiguous from 1.")
    priorities = [item.get("order") for item in elements if item.get("kind") == "decision_rule"]
    if any(not isinstance(value, int) or value < 1 for value in priorities):
        errors.append("Decision rule priorities must be positive integers.")
    return errors


def expand(payload: dict) -> dict:
    """Expand the compact API representation into the full operational ontology."""
    operation = payload["operation"]
    expanded = {
        "schema_version": payload["schema_version"],
        "operation": {
            "name": operation["name"], "objective": operation["objective"],
            "scope": operation["scope"], "trigger": operation["trigger"],
            "completion_definition": operation["completion_definition"],
            "provenance": _provenance(operation),
        },
        "case_types": [], "actors": [], "systems": [], "inputs": [],
        "lifecycle": [], "decision_rules": [], "exceptions": [],
        "escalations": [], "constraints": [], "outcomes": [], "metrics": [],
        "feedback_loops": [], "ambiguities": [], "missing_knowledge": [],
    }
    for item in payload.get("elements") or []:
        base = {"id": item["id"], "provenance": _provenance(item)}
        kind = item["kind"]
        if kind == "case_type":
            expanded["case_types"].append({**base, "name": item["name"], "description": item["summary"], "entry_conditions": item["details"]})
        elif kind == "actor":
            expanded["actors"].append({**base, "name": item["name"], "responsibilities": item["details"], "accountability": item["owner"] or item["summary"]})
        elif kind == "system":
            expanded["systems"].append({**base, "name": item["name"], "purpose": item["summary"]})
        elif kind == "input":
            expanded["inputs"].append({**base, "name": item["name"], "description": item["summary"], "required": item["required"], "used_in": item["links"]})
        elif kind == "stage":
            expanded["lifecycle"].append({**base, "name": item["name"], "order": item["order"], "trigger": item["condition"], "required_information": item["links"], "actions": [value for value in [item["action"], *item["details"]] if value], "decisions": [], "outputs": [item["summary"]] if item["summary"] else [], "owner": item["owner"], "systems": []})
        elif kind == "decision_rule":
            expanded["decision_rules"].append({**base, "name": item["name"], "condition": item["condition"], "action": item["action"], "priority": item["order"], "applies_to": item["links"]})
        elif kind == "exception":
            expanded["exceptions"].append({**base, "trigger": item["condition"], "handling": item["action"], "owner": item["owner"]})
        elif kind == "escalation":
            expanded["escalations"].append({**base, "trigger": item["condition"], "owner": item["owner"], "action": item["action"]})
        elif kind == "constraint":
            expanded["constraints"].append({**base, "category": item["name"], "statement": item["summary"] or item["condition"]})
        elif kind == "outcome":
            expanded["outcomes"].append({**base, "name": item["name"], "definition": item["summary"]})
        elif kind == "metric":
            expanded["metrics"].append({**base, "name": item["name"], "definition": item["summary"], "data_required": item["details"]})
        elif kind == "feedback_loop":
            expanded["feedback_loops"].append({**base, "signal": item["condition"], "review": item["summary"], "improvement_action": item["action"]})
        elif kind == "ambiguity":
            expanded["ambiguities"].append({"id": item["id"], "issue": item["summary"], "question": item["action"], "options": item["details"], "evidence": item["evidence"]})
        elif kind == "missing_knowledge":
            expanded["missing_knowledge"].append({"id": item["id"], "topic": item["name"], "why_it_matters": item["summary"], "question": item["action"], "blocking": item["required"]})
    expanded["lifecycle"].sort(key=lambda item: item["order"])
    expanded["decision_rules"].sort(key=lambda item: item["priority"])
    return expanded


def to_application_payload(payload: dict) -> dict:
    """Project grammar v2 into the current application contract without losing it."""
    ontology = expand(payload)

    def origin(item: dict) -> str:
        source_type = (item.get("provenance") or {}).get("source_type")
        return "knowledge" if source_type == "explicit" else "model_derived"

    operation = ontology["operation"]
    case_types = [
        {"id": item["id"], "name": item["name"], "description": item["description"], "keywords": item.get("entry_conditions") or [], "origin": origin(item), "evidence": (item.get("provenance") or {}).get("evidence") or [], "provenance": item.get("provenance") or {}}
        for item in ontology["case_types"]
    ]
    fields = [
        {"id": item["id"], "label": item["name"], "required": item.get("required", True), "origin": origin(item), "evidence": (item.get("provenance") or {}).get("evidence") or [], "provenance": item.get("provenance") or {}}
        for item in ontology["inputs"]
    ]
    rules = [
        {"id": item["id"], "label": item["name"], "statement": item["condition"], "condition": item["condition"], "action": item["action"], "source": "Ricostruzione operativa di Claude", "confidence": (item.get("provenance") or {}).get("confidence", 0), "status": "draft" if (item.get("provenance") or {}).get("requires_confirmation") else "active", "origin": origin(item), "evidence": (item.get("provenance") or {}).get("evidence") or [], "provenance": item.get("provenance") or {}}
        for item in ontology["decision_rules"]
    ]
    escalations = [
        {"id": item["id"], "trigger": item["trigger"], "owner": item["owner"], "action": item["action"], "origin": origin(item), "evidence": (item.get("provenance") or {}).get("evidence") or [], "provenance": item.get("provenance") or {}}
        for item in ontology["escalations"]
    ]
    clarifications = [
        {"issue_type": item["id"], "question": item["question"], "options": item.get("options") or ["Aggiungi alla procedura", "Lascia irrisolto"], "details": {"issue": item["issue"], "evidence": item.get("evidence") or []}}
        for item in ontology["ambiguities"]
    ]
    clarifications.extend(
        {"issue_type": item["id"], "question": item["question"], "options": ["Aggiungi la regola mancante", "Mantieni il punto aperto"], "details": {"topic": item["topic"], "why_it_matters": item["why_it_matters"], "blocking": item["blocking"]}}
        for item in ontology["missing_knowledge"] if item["blocking"]
    )
    return {
        "operation": {"name": operation["name"], "purpose": operation["objective"]},
        "case_types": case_types, "required_fields": fields, "rules": rules,
        "escalations": escalations, "clarifications": clarifications,
        "ontology": ontology,
    }
