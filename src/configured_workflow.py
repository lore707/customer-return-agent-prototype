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
    "informazioni_richieste": "Informazioni richieste",
    "assegnato": "Assegnato",
    "approvato": "Approvato",
    "rifiutato": "Rifiutato",
    "escalation": "Inoltrato al responsabile",
    "completato": "Completato",
}


def workflow_key(operation_id: str) -> str:
    return f"operation:{operation_id}"


def operation_id_from_case(case: dict) -> str | None:
    key = str(case.get("workflow_key") or "")
    return key.split(":", 1)[1] if key.startswith("operation:") else None


def _case_type(message: str, model: dict) -> dict:
    types = model.get("case_types") or []
    lowered = message.casefold()
    semantic_cues = {
        "withdrawal": ("recesso", "ripensamento", "restituire", "reso volontario"),
        "defective_doa": ("doa", "difetto", "difettoso", "non funziona", "guasto", "malfunzionamento"),
        "warranty": ("garanzia", "warranty", "due anni", "24 mesi"),
        "damaged_delivery": ("arrivato rotto", "arrivato danneggiato", "danno da trasporto"),
        "wrong_item": ("articolo errato", "articolo sbagliato", "prodotto sbagliato"),
        "return_logistics": ("rientro", "reso arrivato", "pacco in sede", "tracking", "controllo fisico"),
        "incomplete_escalation": ("manca", "incompleto", "senza prove", "ambiguo", "eccezione", "fuori policy", "non coperto"),
    }

    def score(item: dict) -> int:
        phrases = list(item.get("keywords") or [])
        phrases.extend(semantic_cues.get(item.get("id"), ()))
        name_tokens = re.findall(r"[a-zà-ÿ]{4,}", (item.get("name") or "").casefold())
        phrase_score = sum(4 for phrase in set(phrases) if phrase and phrase in lowered)
        token_score = sum(1 for token in set(name_tokens) if token in lowered)
        return phrase_score + token_score

    if types:
        ranked = sorted(types, key=score, reverse=True)
        return ranked[0]
    return {"id": "unclassified_operation", "name": "Caso da classificare"}


def _field_definitions(model: dict) -> dict:
    return {
        item["id"]: {
            "label": item["label"],
            "question": f"L’informazione «{item['label'].lower()}» è disponibile e verificata per questo caso?",
            "type": "choice",
            "options": [["available", "Disponibile"], ["missing", "Mancante"], ["not_applicable", "Non applicabile"]],
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
            "motivation": f"Devono ancora essere verificate {len(missing)} informazioni necessarie.",
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
            "motivation": f"Il caso non può proseguire finché non vengono fornite queste informazioni: {', '.join(unavailable)}.",
            "next_action": "Richiedere le informazioni mancanti.",
            "draft": f"Grazie per la richiesta. Prima di confermare il prossimo passaggio ci servono: {', '.join(unavailable)}. Quando saranno disponibili, potremo verificare il caso rispetto alla procedura.",
        }
    if case_type.get("id") in {"exception", "urgent", "budget_exception", "incomplete_escalation", "unclassified_operation"}:
        escalation = (model.get("escalations") or [{"owner": "Responsabile del processo", "action": "Sottoporre il caso a revisione."}])[0]
        return {
            **base,
            "eligibility": "manual_review",
            "outcome": "escalation",
            "rule_id": escalation.get("id") or "ESC-01",
            "motivation": escalation.get("trigger") or "Questo caso richiede una responsabilità esplicita fuori dal percorso standard.",
            "next_action": escalation.get("action") or f"Sottoporre il caso a {escalation.get('owner', 'responsabile del processo')}.",
            "draft": f"La richiesta è stata strutturata e le informazioni necessarie sono disponibili. Poiché non rientra nel percorso standard, deve essere revisionata da {escalation.get('owner', 'responsabile del processo')}.",
        }
    rule = (model.get("rules") or [{"id": "RULE-01", "statement": "Seguire la procedura revisionata.", "action": "Procedere con la conferma umana."}])[0]
    return {
        **base,
        "eligibility": "eligible",
        "outcome": "approvato",
        "rule_id": rule.get("id") or "RULE-01",
        "motivation": rule.get("statement") or "Il caso segue il percorso standard revisionato.",
        "next_action": rule.get("action") or "Procedere con la conferma umana.",
        "draft": "La richiesta è stata verificata rispetto alla procedura attiva. Le informazioni necessarie sono complete e la prossima azione consigliata è pronta per la conferma umana.",
    }


def create_case(message: str, operation: dict, *, operator: str = "Operatore dello spazio di lavoro", path=None) -> dict:
    model = operation.get("operational_model") or {}
    if not model.get("operation"):
        raise ValueError("Questa operazione non dispone di un modello operativo attivo.")
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
            "data_source": "Inserimento dell’operatore",
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
        raise ValueError("Questa informazione non fa parte della procedura attiva.")
    value = str(raw_value or "")
    allowed = {item[0] for item in definitions[field]["options"]}
    if value not in allowed:
        raise ValueError("Seleziona uno dei valori disponibili.")
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
        "label": (model.get("operation") or {}).get("name") or "Operazione configurata",
        "short": "Procedura aziendale",
        "description": (model.get("operation") or {}).get("purpose") or "",
        "input_label": "Richiesta operativa",
        "output_label": "Prossima azione consigliata",
        "playbook": (model.get("operation") or {}).get("name") or "Procedura attiva",
        "examples": [],
    }
    return {
        "case": case,
        "question": question,
        "category_label": case_type.get("name") or case.get("return_reason", "Caso operativo").replace("_", " ").title(),
        "outcome_labels": GENERIC_OUTCOMES,
        "workflow": workflow,
        "fact_rows": fact_rows,
    }


def operation_labels(path=None) -> dict[str, str]:
    return {
        workflow_key(operation["id"]): (operation.get("operational_model") or {}).get("operation", {}).get("name") or operation.get("name") or "Operazione configurata"
        for operation in onboarding_store.list_operations(path=path)
    }
