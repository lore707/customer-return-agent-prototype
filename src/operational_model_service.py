"""Provider-neutral service that turns company knowledge into an operational model.

The local provider is intentionally deterministic: it gives the prototype a
real end-to-end behaviour without pretending an external model was called.
It can later be replaced behind ``get_operational_model_service``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _sentences(text: str) -> list[str]:
    values = re.split(r"(?:\r?\n)+|(?<=[.!?;])\s+", text or "")
    return [re.sub(r"^[\s#*\-\d.)]+", "", item).strip() for item in values if len(item.strip()) >= 18]


def _first_sentence(value: str, fallback: str) -> str:
    values = _sentences(value)
    return values[0][:220] if values else fallback


def _operation_name(context: dict) -> str:
    operation = context["operation"]
    if operation.get("name"):
        return operation["name"][:72]
    description = operation.get("description") or "Core operation"
    cleaned = re.sub(
        r"(?i)^(we|noi|l'azienda|la nostra azienda)\s+(want to|wants to|vogliamo|deve|gestisce)\s+",
        "",
        description.strip(),
    )
    return _first_sentence(cleaned, "Core operation")[:72].rstrip(".")


def _all_text(context: dict) -> str:
    values = list(context["operation"].values()) + [
        source.get("content") or "" for source in context.get("knowledge_sources", [])
    ]
    return "\n".join(str(item) for item in values if item)


FIELD_SIGNALS = (
    ("requester", "Requester or owner", ("requester", "richiedente", "cliente", "owner", "referente")),
    ("request_type", "Request type", ("tipo di richiesta", "request type", "categoria", "caso")),
    ("business_reason", "Business reason", ("motivazione", "business reason", "obiettivo", "purpose")),
    ("deadline", "Deadline or timing", ("scadenza", "deadline", "entro", "giorni", "timing")),
    ("budget", "Budget or amount", ("budget", "costo", "importo", "spesa", "amount", "price")),
    ("evidence", "Evidence or attachments", ("allegat", "document", "foto", "evidence", "proof")),
    ("approver", "Approver", ("approv", "manager", "director", "responsabile", "sign-off")),
    ("priority", "Priority and impact", ("priorit", "urgent", "urgen", "impact", "impatto", "critical")),
    ("market", "Market or location", ("country", "countries", "paese", "mercato", "market", "region")),
    ("system_reference", "System reference", ("ticket", "ordine", "project id", "reference", "codice", "record")),
)


def _required_fields(text: str) -> list[dict]:
    lowered = text.casefold()
    selected = []
    for field_id, label, terms in FIELD_SIGNALS:
        if any(term in lowered for term in terms):
            selected.append({"id": field_id, "label": label, "required": True})
    defaults = [
        {"id": "request_type", "label": "Request type", "required": True},
        {"id": "business_reason", "label": "Business reason", "required": True},
        {"id": "requester", "label": "Requester or owner", "required": True},
    ]
    ids = {item["id"] for item in selected}
    for item in defaults:
        if item["id"] not in ids:
            selected.append(item)
    return selected[:8]


def _extract_rules(text: str, sources: list[dict]) -> list[dict]:
    signal = re.compile(
        r"(?i)\b(if|when|only|must|should|cannot|before|after|requires?|"
        r"se|quando|solo|deve|devono|non pu[oò]|prima|dopo|richiede|entro|oltre)\b"
    )
    source_by_sentence: list[tuple[str, str]] = []
    for source in sources:
        for sentence in _sentences(source.get("content") or ""):
            source_by_sentence.append((sentence, source.get("name") or "Knowledge source"))
    if not source_by_sentence:
        source_by_sentence = [(item, "Operation description") for item in _sentences(text)]

    rules = []
    seen = set()
    for sentence, source_name in source_by_sentence:
        key = sentence.casefold()
        if not signal.search(sentence) or key in seen:
            continue
        seen.add(key)
        rules.append(
            {
                "id": f"RULE-{len(rules) + 1:02d}",
                "statement": sentence[:280],
                "action": sentence[:180],
                "source": source_name,
                "confidence": 0.86,
                "status": "active",
            }
        )
        if len(rules) == 10:
            break
    if not rules:
        rules.append(
            {
                "id": "RULE-01",
                "statement": "A case can proceed only when the required information is available.",
                "action": "Collect missing information before proposing the next step.",
                "source": "Generated operating safeguard",
                "confidence": 0.78,
                "status": "active",
            }
        )
    if not any("approv" in item["statement"].casefold() or "human" in item["statement"].casefold() for item in rules):
        rules.append(
            {
                "id": f"RULE-{len(rules) + 1:02d}",
                "statement": "External actions require human confirmation.",
                "action": "Prepare the recommendation and wait for an operator decision.",
                "source": "Workspace safety default",
                "confidence": 1.0,
                "status": "active",
            }
        )
    return rules


def _case_types(text: str, operation_name: str) -> list[dict]:
    lowered = text.casefold()
    values = [
        ("standard", "Standard request", "Complete request that follows the documented path."),
        ("incomplete", "Incomplete request", "One or more required facts are missing."),
        ("exception", "Policy exception", "The request falls outside a documented rule."),
    ]
    if any(term in lowered for term in ("urgent", "urgen", "critical", "critico", "incident")):
        values.append(("urgent", "Urgent or high-impact case", "A time-sensitive or high-impact request."))
    return [
        {"id": key, "name": label, "description": description, "operation": operation_name}
        for key, label, description in values
    ]


def _escalations(text: str) -> list[dict]:
    lowered = text.casefold()
    roles = []
    for role, terms in (
        ("Manager", ("manager", "responsabile", "lead")),
        ("Finance", ("finance", "amministrazione", "budget")),
        ("Legal or compliance", ("legal", "compliance", "legale")),
        ("Process owner", ("owner", "titolare del processo")),
    ):
        if any(term in lowered for term in terms):
            roles.append(role)
    if not roles:
        roles = ["Process owner"]
    return [
        {
            "id": f"ESC-{index:02d}",
            "trigger": "Ambiguity, exception or risk outside the standard path",
            "owner": role,
            "action": f"Escalate for review by {role}.",
        }
        for index, role in enumerate(roles[:3], 1)
    ]


def _clarifications(context: dict, text: str, fields: list[dict], rules: list[dict]) -> list[dict]:
    issues = []
    money = sorted(set(re.findall(r"(?:€|EUR\s*)\s?(\d[\d.,]*)", text, flags=re.IGNORECASE)))
    if len(money) > 1:
        issues.append(
            {
                "issue_type": "threshold_conflict",
                "question": "Which amount should trigger approval or escalation?",
                "options": [f"Use {value} EUR" for value in money[:3]],
                "details": {"detected_values": money[:5]},
            }
        )
    if not any(item["id"] == "approver" for item in fields):
        issues.append(
            {
                "issue_type": "missing_owner",
                "question": "Who should review exceptions or ambiguous cases?",
                "options": ["Process owner", "Team manager", "Case-by-case"],
                "details": {},
            }
        )
    if len(context["company"].get("markets") or []) > 1 and not any("market" in item["statement"].casefold() for item in rules):
        issues.append(
            {
                "issue_type": "market_scope",
                "question": "Should the same rules apply across all selected markets?",
                "options": ["Use one global playbook", "Review market by market", "Not decided yet"],
                "details": {"markets": context["company"]["markets"]},
            }
        )
    if not context.get("knowledge_sources"):
        issues.append(
            {
                "issue_type": "knowledge_gap",
                "question": "The first model is based only on your description. How should it be treated?",
                "options": ["Use as a draft", "Require playbook review", "Add documents later"],
                "details": {},
            }
        )
    return issues[:4]


@dataclass
class LocalOperationalModelService:
    provider_name: str = "local_structuring_engine"

    def build(self, context: dict) -> dict:
        operation_name = _operation_name(context)
        text = _all_text(context)
        fields = _required_fields(text)
        rules = _extract_rules(text, context.get("knowledge_sources", []))
        escalations = _escalations(text)
        clarifications = _clarifications(context, text, fields, rules)
        case_types = _case_types(text, operation_name)
        source_count = len(context.get("knowledge_sources", []))
        completeness = min(96, 54 + source_count * 8 + min(len(rules), 6) * 3 + min(len(fields), 6) * 2)
        if clarifications:
            completeness -= min(16, len(clarifications) * 4)
        remaining = [item["question"] for item in clarifications]
        if source_count == 0:
            remaining.append("Add at least one source document when it becomes available.")
        company = context["company"]
        policies = [
            {
                "id": f"POL-{index:02d}",
                "name": source.get("name") or f"Knowledge source {index}",
                "source_id": source.get("id"),
                "status": "structured",
            }
            for index, source in enumerate(context.get("knowledge_sources", []), 1)
        ]
        model = {
            "schema_version": "1.0",
            "provider": self.provider_name,
            "operation": {
                "name": operation_name,
                "purpose": _first_sentence(
                    context["operation"].get("objective") or "",
                    f"Create a consistent and reviewable path for {operation_name.lower()}.",
                ),
            },
            "playbook": {
                "name": f"{operation_name} Playbook",
                "version": "1.0",
                "status": "review",
            },
            "policies": policies,
            "company_context": {
                "summary": _first_sentence(
                    company.get("description") or "",
                    f"{company.get('name') or 'The company'} is configuring its first operational workflow.",
                ),
                "industry": company.get("industry"),
                "markets": company.get("markets") or [],
                "business_model": company.get("business_model"),
                "team_size": company.get("team_size"),
            },
            "case_types": case_types,
            "required_fields": fields,
            "rules": rules,
            "escalations": escalations,
            "ambiguities": clarifications,
            "knowledge": {
                "source_count": source_count,
                "source_names": [item.get("name") for item in context.get("knowledge_sources", [])],
                "privacy": context.get("privacy") or {},
            },
            "completeness": max(38, completeness),
            "remaining_setup": remaining[:5],
            "learning": {"decisions_can_be_promoted_to_rules": True},
        }
        return {"model": model, "clarifications": clarifications}

    def scenarios(self, model: dict) -> list[dict]:
        operation = model.get("operation") or {}
        name = operation.get("name") or "the operation"
        fields = model.get("required_fields") or []
        rules = model.get("rules") or []
        primary_rule = rules[0] if rules else {"id": "RULE-01", "action": "Follow the standard path."}
        missing_label = (fields[0].get("label") if fields else "Required information")
        escalation = (model.get("escalations") or [{"owner": "Process owner"}])[0]
        return [
            {
                "title": "Complete standard case",
                "input_summary": f"A routine {name.lower()} request includes every required field and matches the documented process.",
                "recommendation": primary_rule.get("action") or "Proceed through the standard path.",
                "rationale": f"Applies {primary_rule.get('id', 'the primary rule')} with human confirmation before execution.",
            },
            {
                "title": f"Missing {missing_label.lower()}",
                "input_summary": f"The request is relevant to {name.lower()}, but {missing_label.lower()} is not available.",
                "recommendation": f"Request {missing_label.lower()} before deciding.",
                "rationale": "Incomplete inputs should not be converted into confident operational decisions.",
            },
            {
                "title": "Exception outside the documented path",
                "input_summary": "The request conflicts with a rule or introduces an exception that is not fully documented.",
                "recommendation": f"Escalate to {escalation.get('owner', 'the process owner')} for review.",
                "rationale": "The model preserves ownership when the playbook does not provide a safe deterministic outcome.",
            },
        ]

    def resolve(self, model: dict, answers: list[dict]) -> dict:
        resolved = {item.get("issue_type"): item.get("answer") for item in answers if item.get("answer")}
        updated = dict(model)
        updated["clarification_answers"] = resolved
        updated["ambiguities"] = [
            {**item, "resolved": item.get("issue_type") in resolved, "answer": resolved.get(item.get("issue_type"))}
            for item in model.get("ambiguities", [])
        ]
        unresolved = [item for item in updated["ambiguities"] if not item.get("resolved")]
        updated["remaining_setup"] = [item.get("question") for item in unresolved]
        updated["completeness"] = min(98, int(model.get("completeness") or 50) + len(resolved) * 5)
        return updated


def get_operational_model_service() -> LocalOperationalModelService:
    """Factory boundary for a future external AI-backed implementation."""

    return LocalOperationalModelService()
