"""Run cases against an operational model created during onboarding."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

import database
import domain
import onboarding_store
from context_privacy import _redact


GENERIC_OUTCOMES = {
    "informazioni_richieste": "Information requested",
    "assegnato": "Assigned",
    "approvato": "Approved",
    "rifiutato": "Rejected",
    "escalation": "Escalated",
    "completato": "Completed",
}


def workflow_key(operation_id: str) -> str:
    return f"operation:{operation_id}"


def operation_id_from_case(case: dict) -> str | None:
    key = str(case.get("workflow_key") or "")
    return key.split(":", 1)[1] if key.startswith("operation:") else None


def _case_type(message: str, model: dict) -> dict:
    types = model.get("case_types") or []
    lowered = message.casefold()
    target = "standard"
    if any(term in lowered for term in ("exception", "eccezione", "deroga", "outside", "fuori processo")):
        target = "exception"
    elif any(term in lowered for term in ("urgent", "urgente", "critical", "critico", "bloccato")):
        target = "urgent"
    elif any(term in lowered for term in ("missing", "manca", "incomplete", "incompleto")):
        target = "incomplete"
    return next((item for item in types if item.get("id") == target), types[0] if types else {"id": "standard", "name": "Standard request"})


def _field_definitions(model: dict) -> dict:
    return {
        item["id"]: {
            "label": item["label"],
            "question": f"Is {item['label'].lower()} available and verified for this case?",
            "type": "choice",
            "options": [["available", "Available"], ["missing", "Missing"], ["not_applicable", "Not applicable"]],
        }
        for item in model.get("required_fields") or []
    }


def _evaluate(case_type: dict, facts: dict, model: dict) -> dict:
    fields = _field_definitions(model)
    missing = [field for field in fields if field not in facts]
    base = {
        "field_definitions": fields,
        "operation": model.get("operation") or {},
        "case_type": case_type,
        "missing": missing,
    }
    if missing:
        return {
            **base,
            "eligibility": "needs_information",
            "outcome": "raccogli_contesto",
            "rule_id": "INTAKE-01",
            "motivation": f"{len(missing)} required fields still need verification.",
            "next_action": fields[missing[0]]["question"],
            "draft": None,
        }
    unavailable = [fields[key]["label"] for key, value in facts.items() if value == "missing" and key in fields]
    if unavailable:
        return {
            **base,
            "eligibility": "needs_information",
            "outcome": "informazioni_richieste",
            "rule_id": "INTAKE-02",
            "motivation": f"The case cannot proceed until the missing information is supplied: {', '.join(unavailable)}.",
            "next_action": "Ask for the missing information.",
            "draft": f"Thanks for the request. Before we can confirm the next step, we need: {', '.join(unavailable)}. Once available, the case can be reviewed against the playbook.",
        }
    if case_type.get("id") in {"exception", "urgent"}:
        escalation = (model.get("escalations") or [{"owner": "Process owner", "action": "Escalate for review."}])[0]
        return {
            **base,
            "eligibility": "manual_review",
            "outcome": "escalation",
            "rule_id": escalation.get("id") or "ESC-01",
            "motivation": escalation.get("trigger") or "This case requires explicit ownership outside the standard path.",
            "next_action": escalation.get("action") or f"Escalate to {escalation.get('owner', 'the process owner')}.",
            "draft": f"The request has been structured and the required information is available. Because it falls outside the standard path, it should now be reviewed by {escalation.get('owner', 'the process owner')}.",
        }
    rule = (model.get("rules") or [{"id": "RULE-01", "statement": "Follow the reviewed playbook.", "action": "Proceed with human confirmation."}])[0]
    return {
        **base,
        "eligibility": "eligible",
        "outcome": "approvato",
        "rule_id": rule.get("id") or "RULE-01",
        "motivation": rule.get("statement") or "The case follows the reviewed standard path.",
        "next_action": rule.get("action") or "Proceed with human confirmation.",
        "draft": "The request has been reviewed against the current playbook. The required information is complete and the recommended next action is ready for human confirmation.",
    }


def create_case(message: str, operation: dict, *, operator: str = "Workspace operator", path=None) -> dict:
    model = operation.get("operational_model") or {}
    if not model.get("operation"):
        raise ValueError("This operation does not have an active operational model.")
    sanitized, redactions = _redact(message)
    sanitized = sanitized[:5000]
    case_type = _case_type(sanitized, model)
    result = _evaluate(case_type, {}, model)
    case = database.create_case(
        {
            "id": f"CS-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
            "session_id": f"workspace-{uuid.uuid4().hex}",
            "request_date": database.utc_now(),
            "return_type": "operations_case",
            "return_reason": case_type.get("id") or "standard",
            "detailed_reason": sanitized,
            "customer_message": sanitized,
            "ai_classification": {"category": case_type.get("id"), "label": case_type.get("name"), "confidence": 0.86},
            "confidence": 0.86,
            "eligibility_result": result["eligibility"],
            "policy_applied": result["motivation"],
            "policy_decision": result,
            "suggested_resolution": result["outcome"],
            "original_suggested_response": result["draft"],
            "analysis_duration_ms": 480,
            "data_source": "Operator input",
            "source_mode": "policy_copilot_configured",
            "workflow_key": workflow_key(operation["id"]),
            "source_fetched_at": database.utc_now(),
            "source_payload": {"privacy_mode": True, "redactions": redactions, "operation_id": operation["id"]},
            "case_facts": {},
            "missing_information": result["missing"],
            "privacy_mode": 1,
            "assigned_operator": operator,
            "ai_mode": "configured_local_service",
        },
        path=path,
    )
    case = database.transition_case(case["id"], domain.CaseStatus.ANALYZED.value, event_type="message_analyzed", details={"case_type": case_type.get("id"), "redactions": redactions}, path=path)
    return database.transition_case(
        case["id"],
        domain.CaseStatus.NEEDS_INFORMATION.value if result["missing"] else domain.CaseStatus.WAITING_HUMAN_APPROVAL.value,
        event_type="context_requested" if result["missing"] else "policy_decision_ready",
        details={"missing": result["missing"], "operation_id": operation["id"]},
        path=path,
    )


def update_fact(case_id: str, field: str, raw_value, *, path=None) -> dict:
    case = database.get_case(case_id, path)
    operation_id = operation_id_from_case(case or {})
    operation = onboarding_store.get_operation(operation_id, path)
    if not case or not operation:
        raise KeyError(case_id)
    model = operation.get("operational_model") or {}
    definitions = _field_definitions(model)
    if field not in definitions:
        raise ValueError("This information is not part of the active playbook.")
    value = str(raw_value or "")
    allowed = {item[0] for item in definitions[field]["options"]}
    if value not in allowed:
        raise ValueError("Select one of the available values.")
    facts = {**(case.get("case_facts") or {}), field: value}
    case_type = (case.get("policy_decision") or {}).get("case_type") or _case_type(case["customer_message"], model)
    result = _evaluate(case_type, facts, model)
    updated = database.update_case(
        case_id,
        {
            "case_facts": facts,
            "missing_information": result["missing"],
            "eligibility_result": result["eligibility"],
            "policy_applied": result["motivation"],
            "policy_decision": result,
            "suggested_resolution": result["outcome"],
            "original_suggested_response": result["draft"],
        },
        event_type="case_fact_recorded",
        event_details={"field": field, "value": value, "operation_id": operation_id},
        path=path,
    )
    if not result["missing"] and updated["status"] == domain.CaseStatus.NEEDS_INFORMATION.value:
        updated = database.transition_case(
            case_id, domain.CaseStatus.WAITING_HUMAN_APPROVAL.value,
            event_type="policy_decision_ready", details={"rule_id": result["rule_id"], "operation_id": operation_id}, path=path,
        )
    return updated


def view_model(case: dict) -> dict:
    operation_id = operation_id_from_case(case)
    operation = onboarding_store.get_operation(operation_id)
    model = (operation or {}).get("operational_model") or {}
    decision = case.get("policy_decision") or {}
    definitions = decision.get("field_definitions") or _field_definitions(model)
    missing = case.get("missing_information") or []
    fact_rows = []
    for field, value in (case.get("case_facts") or {}).items():
        definition = definitions.get(field, {"label": field, "options": []})
        displayed = dict(definition.get("options") or []).get(str(value), value)
        fact_rows.append({"id": field, "label": definition.get("label", field), "value": displayed})
    question = {"id": missing[0], **definitions[missing[0]]} if missing and missing[0] in definitions else None
    case_type = decision.get("case_type") or {}
    workflow = {
        "key": case.get("workflow_key"),
        "label": (model.get("operation") or {}).get("name") or "Configured operation",
        "short": "Company playbook",
        "description": (model.get("operation") or {}).get("purpose") or "",
        "input_label": "Operational request",
        "output_label": "Recommended next action",
        "playbook": (model.get("operation") or {}).get("name") or "Active playbook",
        "examples": [],
    }
    return {
        "case": case,
        "question": question,
        "category_label": case_type.get("name") or case.get("return_reason", "Operational case").replace("_", " ").title(),
        "outcome_labels": GENERIC_OUTCOMES,
        "workflow": workflow,
        "fact_rows": fact_rows,
    }


def operation_labels(path=None) -> dict[str, str]:
    return {
        workflow_key(operation["id"]): (operation.get("operational_model") or {}).get("operation", {}).get("name") or operation.get("name") or "Configured operation"
        for operation in onboarding_store.list_operations(path=path)
    }
