"""Build an evidence-aware Operational Model from company knowledge.

The extraction contract is domain neutral. A local deterministic provider keeps
the prototype usable without paid calls; an Anthropic provider can perform the
same job for arbitrary operations when explicitly enabled. Both providers feed
the same validation, completeness and persistence pipeline.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

import anthropic
import operational_grammar


LOGGER = logging.getLogger(__name__)
MODEL = os.getenv("OPERATIONAL_MODEL_MODEL", "claude-sonnet-5")
MODEL_EFFORT = os.getenv("OPERATIONAL_MODEL_EFFORT", "medium").strip().lower()
if MODEL_EFFORT not in {"low", "medium", "high"}:
    MODEL_EFFORT = "medium"
try:
    MODEL_MAX_TOKENS = max(4_000, min(12_000, int(os.getenv("OPERATIONAL_MODEL_MAX_TOKENS", "12000"))))
except ValueError:
    MODEL_MAX_TOKENS = 12_000


SYSTEM_PROMPT = """You are an Operational Reconstruction Engine.

Reconstruct how the supplied company actually works. The JSON schema is the universal
grammar; none of its business content is preconfigured. Never force the material into generic
categories such as "standard request", "incomplete request" or "policy exception" when the
evidence supports domain-specific concepts.

Reason across the company context, goals, current process and all knowledge sources. Reconstruct
actors, systems, inputs, lifecycle stages, decisions, outputs, constraints, exceptions, metrics
and feedback loops. Lifecycle stages must describe the actual operation, not a generic template.

The output schema uses a compact typed-element transport. Create one element for every relevant
operational object and use these field conventions:
- operational_domain: summary=what the area does, action=its objective, details=main activities.
- case_type: summary=definition, details=entry conditions.
- actor: details=responsibilities, owner=accountability.
- system/input: summary=purpose or definition; input.required marks required facts.
- stage: condition=trigger, action=primary action, details=other actions, summary=output,
  owner=responsible actor, links=required input IDs, order=1..N.
- decision_rule: condition=IF, action=THEN, links=case/stage IDs, order=priority starting at 1.
- exception/escalation: condition=trigger, action=handling, owner=human owner.
- constraint/outcome/metric: summary=clear definition; metric.details=data needed.
- feedback_loop: condition=signal, summary=review cadence, action=improvement action.
- ambiguity/missing_knowledge: summary=why it matters, action=the exact question;
  details=answer options, and required=true only when blocking.
Use empty strings, empty arrays, false and order=0 for fields that do not apply. Links contain IDs
from this same output. Do not omit a real object merely because the transport is compact.

Evidence discipline for every item:
- explicit: directly stated; quote a short, exact excerpt as evidence.
- derived: logically reconstructed from multiple supplied facts; cite those excerpts and explain
  the result in the item's normal descriptive fields.
- suggested: a useful industry/process recommendation not supported by supplied facts. It must
  have requires_confirmation=true and must never be phrased as an active company rule.
- Never invent thresholds, legal requirements, owners, systems or outcomes.
- Missing knowledge becomes missing_knowledge. Contradictions become ambiguities.
- An empty array is better than a fabricated fact.
- External actions always retain a human confirmation boundary.

Quality requirements:
- Prefer the terminology used by the company.
- Identify the real operational domains implied by the company description, activities and sources.
  Do not confuse departments with recurring work areas and do not invent unsupported domains.
- A case type is a recurring operational path, not a completeness state.
- Rules must have executable condition/action semantics.
- Stages must be ordered, non-overlapping and collectively explain the end-to-end flow.
- Infer carefully from incomplete prose, but expose every inference through provenance.
- Write every user-facing label and description in Italian, regardless of the source language.
- Be complete without being repetitive: return at most 32 elements, normally 4-8 stages,
  3-10 decision rules and only distinct actors, systems, inputs and controls.
- Keep descriptive strings concise, details to at most 3 entries and evidence to one short
  source excerpt per element. Missing knowledge is more useful than duplicated filler.
"""


def _sentences(text: str) -> list[str]:
    values = re.split(r"(?:\r?\n)+|(?<=[.!?;])\s+", text or "")
    return [
        re.sub(r"^[\s#*\-\d.)]+", "", item).strip()
        for item in values
        if len(item.strip()) >= 14
    ]


def _first_sentence(value: str, fallback: str) -> str:
    values = _sentences(value)
    return values[0][:220] if values else fallback


def _all_text(context: dict) -> str:
    values = list(context["operation"].values()) + [
        source.get("content") or "" for source in context.get("knowledge_sources", [])
    ]
    return "\n".join(str(item) for item in values if item)


def _clean_markdown(value: str) -> str:
    value = re.sub(r"[`*_#]", "", str(value or ""))
    value = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", value)
    return re.sub(r"\s+", " ", value).strip(" -–—:;.|\t")


def _slug(value: str, fallback: str) -> str:
    cleaned = value.casefold().replace("à", "a").replace("è", "e").replace("é", "e").replace("ì", "i").replace("ò", "o").replace("ù", "u")
    return re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")[:52] or fallback


def _dedupe(items: list[dict], key: str) -> list[dict]:
    result = []
    seen = set()
    for item in items:
        value = re.sub(r"\W+", " ", str(item.get(key) or "").casefold()).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(item)
    return result


def _source_for_excerpt(excerpt: str, sources: list[dict]) -> str:
    words = [word for word in re.findall(r"\w+", excerpt.casefold()) if len(word) > 4]
    for source in sources:
        content = (source.get("content") or "").casefold()
        if excerpt.casefold() in content or (words and sum(word in content for word in words[:8]) >= min(3, len(words))):
            return source.get("name") or "Fonte di conoscenza"
    return "Descrizione dell’operazione"


def _operation_name(context: dict, text: str) -> str:
    operation = context["operation"]
    if operation.get("name"):
        return operation["name"][:72]
    description = _first_sentence(operation.get("description") or "", "Operazione principale")
    cleaned = re.sub(
        r"(?i)^(?:we|noi|l'azienda|la nostra azienda)?\s*(?:want to|wants to|vogliamo|deve|gestisce|gestire|manage|handle|coordinate|coordinare)\s+",
        "",
        description,
    )
    cleaned = re.split(r"(?i)\s+(?:dall['’]|from|so that|affinché|per poter)\b", cleaned, maxsplit=1)[0]
    cleaned = _clean_markdown(cleaned).rstrip(".")
    return cleaned[:72] or "Core operation"


def _split_list(value: str) -> list[str]:
    value = re.split(r"(?i)\b(?:prima di|before|affinché|so that)\b", value, maxsplit=1)[0]
    parts = re.split(r"\s*[,;]\s*|\s+(?:e|ed|and)\s+", value)
    cleaned = []
    for part in parts:
        item = _clean_markdown(part)
        item = re.sub(r"(?i)^(?:il|lo|la|i|gli|le|un|uno|una|the|a|an)\s+", "", item)
        item = re.sub(r"(?i)\s+(?:che|which|that)\s+.*$", "", item)
        if 1 <= len(item.split()) <= 10 and 2 < len(item) <= 90:
            cleaned.append(item)
    return cleaned


def _table_case_types(text: str) -> list[dict]:
    result = []
    active = False
    for raw in text.splitlines():
        if not raw.lstrip().startswith("|"):
            active = False
            continue
        cells = [_clean_markdown(cell) for cell in raw.strip().strip("|").split("|")]
        if not cells:
            continue
        first = cells[0].casefold()
        if first in {"categoria", "category", "case type", "tipo richiesta", "tipo di richiesta"}:
            active = True
            continue
        if not active or not first or re.fullmatch(r"[-: ]+", cells[0]):
            continue
        result.append(
            {
                "name": cells[0][:80],
                "description": (cells[1] if len(cells) > 1 else "Percorso operativo descritto nella conoscenza fornita.")[:180],
                "keywords": [cells[0].casefold()],
                "evidence": [" | ".join(cells[:3])[:220]],
            }
        )
    return result


def _declared_case_types(text: str) -> list[dict]:
    result = []
    pattern = re.compile(
        r"(?im)(?:distinguiamo|tipi di (?:caso|richiesta)|case types?(?: include)?|categorie(?: operative)?)\s*(?:sono|includono|:)?\s*([^\n.]{5,260})"
    )
    for match in pattern.finditer(text):
        for label in _split_list(match.group(1)):
            result.append(
                {
                    "name": label[:80],
                    "description": "Percorso operativo esplicitamente indicato nella conoscenza fornita.",
                    "keywords": [label.casefold()],
                    "evidence": [_clean_markdown(match.group(0))[:220]],
                }
            )
    escalation_pattern = re.compile(
        r"(?im)\b((?:un|il|ogni)?\s*cas[oi]\s+[^.\n]{3,100}?)\s+(?:deve|viene|va)\s+(?:essere\s+)?escalat\w*"
    )
    for match in escalation_pattern.finditer(text):
        label = _clean_markdown(match.group(1))
        result.append(
            {
                "name": label[:80],
                "description": "Percorso che richiede ownership umana secondo la conoscenza fornita.",
                "keywords": [label.casefold()],
                "evidence": [_clean_markdown(match.group(0))[:220]],
            }
        )
    return result


def _heading_case_types(text: str) -> list[dict]:
    result = []
    in_section = False
    for raw in text.splitlines():
        heading = re.match(r"^#{1,4}\s+(.+)$", raw.strip())
        if heading:
            title = _clean_markdown(heading.group(1))
            in_section = bool(re.search(r"(?i)\b(case types?|tipi di (?:caso|richiesta)|categorie di richiesta)\b", title))
            continue
        if not in_section:
            continue
        bullet = re.match(r"^\s*[-*]\s+([^:–—|]{2,80})(?:\s*[:–—].*)?$", raw)
        if bullet:
            label = _clean_markdown(bullet.group(1))
            result.append(
                {
                    "name": label,
                    "description": "Percorso operativo elencato nella conoscenza fornita.",
                    "keywords": [label.casefold()],
                    "evidence": [_clean_markdown(raw)],
                }
            )
    return result


def _implicit_case_types(text: str) -> list[dict]:
    """Discover recurring source terminology without relying on a domain catalogue."""
    candidates: list[tuple[str, str]] = []
    patterns = (
        r"(?i)\b(?:gestire|gestiamo|manage|handle|coordinate)\s+([^.!?\n]{3,150})",
        r"(?i)\b(?:richiesta|request|caso|case|pratica|ticket)\s+(?:di|per|for)?\s*([\wÀ-ÿ/-]+(?:\s+[\wÀ-ÿ/-]+){0,3})",
        r"(?im)^(?:per\s+)?(?:un|una|il|la|the|a|an)?\s*([A-Z]{2,12}|[A-Za-zÀ-ÿ]+(?:\s+[A-Za-zÀ-ÿ/]+){0,2})\s+(?:è|sono|is|are|richiede|requires|prevede|copre|must|deve)\b",
        r"(?i)\b(?:per|se)\s+(?:il|la|un|una|the|a|an)\s+([\wÀ-ÿ/-]+(?:\s+[\wÀ-ÿ/-]+){0,3}?)\s+(?:si\b|è\b|is\b|are\b|richiede\b|requires\b)",
    )
    stop_phrases = {
        "cliente", "customer", "operatore", "operator", "manager", "responsabile",
        "team", "richiesta", "request", "caso", "case", "processo", "process",
        "sistema", "system", "prodotto", "product", "informazioni", "information",
    }
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            raw_label = _clean_markdown(match.group(1))
            manage_declaration = bool(re.match(r"(?i)^(?:gestire|gestiamo|manage|handle|coordinate)", _clean_markdown(match.group(0))))
            if manage_declaration and not re.search(r"[,;/]|\s+(?:e|ed|and)\s+", raw_label, flags=re.IGNORECASE):
                continue
            for list_label in (_split_list(raw_label) if manage_declaration else [raw_label]):
                label = re.split(
                r"(?i)\b(?:che|quando|where|which|with|con|senza|deve|must|arriva|arrive|contiene|include)\b",
                list_label,
                maxsplit=1,
                )[0].strip()
                label = re.split(r"(?i)\s+(?:dall['’]?|from|fino|until)\b", label, maxsplit=1)[0].strip()
                if not label or label.casefold() in stop_phrases or len(label) < 3:
                    continue
                candidates.append((label, _clean_markdown(match.group(0))[:220]))
    counts: dict[str, int] = {}
    for label, _ in candidates:
        counts[label.casefold()] = len(re.findall(rf"(?<!\w){re.escape(label)}(?!\w)", text, flags=re.IGNORECASE))
    result = []
    for label, evidence in candidates:
        declared_by_operation = bool(re.search(rf"(?i)\b(?:gestire|gestiamo|manage|handle|coordinate)\b[^.!?\n]{{0,150}}\b{re.escape(label)}\b", text))
        if counts.get(label.casefold(), 0) < 2 and not re.fullmatch(r"[A-Z0-9/-]{2,16}", label) and not declared_by_operation:
            continue
        result.append(
            {
                "name": label,
                "description": f"Percorso ricorrente associato a {label}, rilevato nella conoscenza fornita.",
                "keywords": [label.casefold()],
                "evidence": [evidence],
            }
        )
    return _dedupe(result, "name")


def _extract_case_types(text: str, operation_name: str) -> tuple[list[dict], bool]:
    extracted = _table_case_types(text) + _declared_case_types(text) + _heading_case_types(text) + _implicit_case_types(text)
    extracted = _dedupe(extracted, "name")[:8]
    specific = []
    for item in extracted:
        words = set(re.findall(r"[a-zà-ÿ0-9]+", item["name"].casefold()))
        mentions = len(re.findall(rf"(?<!\w){re.escape(item['name'])}(?!\w)", text, flags=re.IGNORECASE))
        if len(words) == 1 and len(extracted) > 2 and mentions < 2:
            continue
        if any(words and words.issubset(set(re.findall(r"[a-zà-ÿ0-9]+", kept["name"].casefold()))) for kept in specific):
            continue
        specific.append(item)
    extracted = specific[:8]
    if extracted:
        return [
            {
                "id": _slug(item["name"], f"case_{index}"),
                "name": item["name"],
                "description": item["description"],
                "operation": operation_name,
                "keywords": item.get("keywords") or [],
                "origin": item.get("origin") if item.get("origin") in {"knowledge", "model_derived", "inferred_placeholder", "human_review"} else "knowledge",
                "provenance": item.get("provenance") if isinstance(item.get("provenance"), dict) else {},
                "evidence": item.get("evidence") or [],
            }
            for index, item in enumerate(extracted, 1)
        ], True
    return [
        {
            "id": "unclassified_operation",
            "name": operation_name,
            "description": "The supplied material does not yet expose distinct recurring paths.",
            "operation": operation_name,
            "keywords": [],
            "origin": "inferred_placeholder",
            "evidence": [],
        }
    ], False


def _extract_required_fields(text: str) -> list[dict]:
    candidates = []
    patterns = (
        r"(?i)(?:deve|devono|must|should)\s+(?:includere|include|contenere|contain)\s+([^.;\n]{3,240})",
        r"(?i)(?:comunica|fornisce|recupera|raccoglie|servono|sono obbligatori|required facts?|required information)\s*[:]?\s+([^.;\n]{3,240})",
        r"(?i)(?:conserva|salva|registra)\s+([^.;\n]{3,180})",
        r"(?i)(?:chiede|richiede|domanda|asks? for|requests?)\s+([^.;\n]{3,220})",
        r"(?i)(?:aggiorna|updates?)\s+(?:il\s+)?(?:database|record|ticket|caso|case)?\s*(?:con|with|includendo|including)\s+([^.;\n]{3,220})",
        r"(?i)(?:contesto|context)\s*:\s*[^()\n]*\(([^)]{3,260})\)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            excerpt = _clean_markdown(match.group(0))[:220]
            values = re.sub(r"\s+(?:o|oppure|or)\s+", ", ", match.group(1), flags=re.IGNORECASE)
            for label in _split_list(values):
                label = re.sub(r"(?i)^(?:recupera|raccoglie|verifica|aggiorna|conserva|salva|registra)\s+", "", label)
                candidates.append({"label": label, "evidence": [excerpt]})
    fields = _dedupe(candidates, "label")[:12]
    return [
        {
            "id": _slug(item["label"], f"field_{index}"),
            "label": item["label"],
            "required": True,
            "origin": "knowledge",
            "evidence": item.get("evidence") or [],
        }
        for index, item in enumerate(fields, 1)
    ]


def _rule_prefix(statement: str) -> str:
    lowered = statement.casefold()
    if re.search(r"\bescal\w*|\bambig\w*|\bexception\w*|\beccezion\w*", lowered):
        return "ESC"
    if re.search(r"\bentro\b|\bwithin\b|\bafter\b|\bprima\b|\bdopo\b", lowered):
        return "TIM"
    if re.search(r"\brichied\w*|\bobbligator\w*|\brequires?\b|\bmust\b", lowered):
        return "REQ"
    stop = {"the", "this", "that", "every", "each", "il", "lo", "la", "gli", "le", "un", "una", "ogni", "se", "quando"}
    words = [word for word in re.findall(r"[a-zà-ÿ]{3,}", lowered) if word not in stop]
    return re.sub(r"[^A-Z]", "", (words[0] if words else "rule").upper())[:3] or "OPS"


def _extract_rules(text: str, sources: list[dict]) -> list[dict]:
    signal = re.compile(
        r"(?i)\b(if|when|only|must|should|cannot|before|after|within|requires?|"
        r"se|quando|solo|deve|devono|non pu[oò]|prima|dopo|richiede|entro|oltre|"
        r"obbligator\w*|approv\w*|escal\w*|sostitui\w*|rimbors\w*)\b"
    )
    source_sentences: list[tuple[str, str]] = []
    for source in sources:
        source_sentences.extend(
            (sentence, source.get("name") or "Fonte di conoscenza")
            for sentence in _sentences(source.get("content") or "")
        )
    if not source_sentences:
        source_sentences = [(sentence, "Descrizione dell’operazione") for sentence in _sentences(text)]
    counters: dict[str, int] = {}
    rules = []
    seen = set()
    for raw, source_name in source_sentences:
        statement = _clean_markdown(raw)
        key = statement.casefold()
        has_explicit_amount = bool(re.search(r"(?:€|EUR\s*)\s?\d", statement, flags=re.IGNORECASE))
        if (not signal.search(statement) and not has_explicit_amount) or key in seen:
            continue
        seen.add(key)
        prefix = _rule_prefix(statement)
        counters[prefix] = counters.get(prefix, 0) + 1
        condition, action = _condition_and_action(statement)
        rules.append(
            {
                "id": f"{prefix}-{counters[prefix]:02d}",
                "label": _rule_label(condition, action),
                "statement": statement[:320],
                "condition": condition[:220],
                "action": action[:240],
                "source": source_name,
                "confidence": .86,
                "status": "active",
                "origin": "knowledge",
                "evidence": [statement[:180]],
            }
        )
        if len(rules) >= 16:
            break
    if not rules:
        rules.append(
            {
                "id": "SAFE-01",
                "label": "Contesto completo prima della decisione",
                "statement": "Un caso può procedere solo quando sono disponibili tutte le informazioni necessarie.",
                "action": "Raccogliere le informazioni mancanti prima di proporre il passaggio successivo.",
                "source": "Protezione operativa generata",
                "confidence": .72,
                "status": "draft",
                "origin": "safeguard",
                "evidence": [],
            }
        )
    if not any(re.search(r"(?i)\b(human|operator|operatore|approv)\w*\b", item["statement"]) for item in rules):
        rules.append(
            {
                "id": "SAFE-02",
                "label": "Conferma umana prima dell’esecuzione",
                "statement": "Le azioni esterne richiedono una conferma umana.",
                "action": "Preparare la raccomandazione e attendere la decisione di un operatore.",
                "source": "Protezione predefinita dello spazio di lavoro",
                "confidence": 1.0,
                "status": "active",
                "origin": "safeguard",
                "evidence": [],
            }
        )
    return rules


def _condition_and_action(statement: str) -> tuple[str, str]:
    """Split rough prose into a reviewable condition and next action."""
    cleaned = _clean_markdown(statement)
    conditional = re.match(
        r"(?is)^((?:se|quando|per|if|when|for|entro|oltre|within|above|below)\b[^,:;]{2,180})\s*[,;:]\s*(.+)$",
        cleaned,
    )
    if conditional:
        return conditional.group(1).strip(), conditional.group(2).strip()
    arrow = re.split(r"\s*(?:→|->|=>)\s*", cleaned, maxsplit=1)
    if len(arrow) == 2:
        return arrow[0].strip(), arrow[1].strip()
    normative = re.match(r"(?is)^(.{3,180}?)\s+(deve|devono|va|vanno|must|should)\s+(.+)$", cleaned)
    if normative:
        return normative.group(1).strip(), f"{normative.group(2)} {normative.group(3)}".strip()
    before = re.match(r"(?is)^(.{3,180}?)\s+(?:prima di|before)\s+(.+)$", cleaned)
    if before:
        return before.group(2).strip(), before.group(1).strip()
    return cleaned, cleaned


def _rule_label(condition: str, action: str) -> str:
    action = re.sub(
        r"(?i)^(?:si |il sistema |l['’]operatore )?(?:deve|devono|should|must|può|possono|can)\s+",
        "",
        action,
    )
    candidate = action if action.casefold() != condition.casefold() else condition
    return candidate[:96].rstrip(".,;:")


def _extract_escalations(text: str) -> list[dict]:
    results = []
    patterns = (
        r"(?i)([^.\n]{3,150}?)\s+(?:deve|viene|va)\s+(?:essere\s+)?escalat\w*\s+(?:al|alla|a|to)\s+([\w &/-]{2,50})",
        r"(?i)escalation\s*(?:→|->|:|to)?\s*([\w &/-]{2,50})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            if len(match.groups()) == 2:
                trigger, owner = match.group(1), match.group(2)
            else:
                trigger, owner = "Eccezioni e decisioni non coperte dalla procedura", match.group(1)
            owner = re.split(r"[.;\n]", owner)[0]
            owner = _clean_markdown(owner)
            if not owner:
                continue
            results.append(
                {
                    "trigger": _clean_markdown(trigger)[-180:],
                    "owner": owner[:70],
                    "action": f"Sottoporre il caso alla revisione di {owner[:70]}.",
                    "origin": "knowledge",
                    "evidence": [_clean_markdown(match.group(0))[:220]],
                }
            )
    results = _dedupe(results, "owner")[:4]
    if not results:
        return [
            {
                "id": "ESC-DRAFT",
                "trigger": "Ambiguità, eccezione o rischio fuori dal percorso documentato",
                "owner": "Responsabile da confermare",
                "action": "Sospendere il caso finché non viene definito un responsabile.",
                "origin": "safeguard",
                "evidence": [],
            }
        ]
    return [{"id": f"ESC-{index:02d}", **item} for index, item in enumerate(results, 1)]


ACTION_VERBS = re.compile(
    r"(?i)\b(?:dice|segnala|vuole|chiede|richiede|invia|fornisce|riceve|recupera|controlla|verifica|valuta|"
    r"approva|rifiuta|assegna|aggiorna|registra|crea|carica|comunica|notifica|escal\w*|"
    r"chiude|apre|procede|elabora|emette|paga|rimborsa|sostituisce|duplica|testa|"
    r"says?|reports?|wants?|asks?|requests?|sends?|provides?|receives?|retrieves?|checks?|verifies?|reviews?|"
    r"approves?|rejects?|assigns?|updates?|records?|creates?|uploads?|notifies?|closes?|"
    r"opens?|processes?|issues?|refunds?|replaces?|duplicates?|tests?)\b"
)

ROLE_WORDS = (
    "cliente", "customer", "operatore", "operator", "coordinatore", "coordinator",
    "manager", "responsabile", "owner", "finance", "finanza", "it", "hr",
    "warehouse", "magazzino", "logistica", "logistics", "supplier", "fornitore",
    "dipendente", "employee", "richiedente", "requester", "team", "amministrazione",
)

SYSTEM_WORDS = (
    "shopify", "sendcloud", "make", "zapier", "salesforce", "hubspot", "zendesk",
    "gorgias", "willing", "willdesk", "jira", "notion", "slack", "teams", "email",
    "e-mail", "crm", "erp", "database", "foglio", "spreadsheet", "portale", "portal",
)

CHANNEL_WORDS = {
    "email": ("email", "e-mail", "mail"),
    "chat": ("chat", "messaggio", "message", "whatsapp"),
    "form": ("form", "modulo", "ticket", "portale", "portal"),
    "phone": ("telefono", "phone", "call", "chiamata"),
    "meeting": ("meeting", "riunione", "call interna"),
}


def _sentence_source_rows(context: dict) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    operation = context.get("operation") or {}
    for field, label in (
        ("description", "Descrizione dell’operazione"),
        ("objective", "Obiettivo dell’operazione"),
        ("current_process", "Processo attuale"),
    ):
        rows.extend((sentence, label) for sentence in _sentences(operation.get(field) or ""))
    for source in context.get("knowledge_sources") or []:
        rows.extend(
            (sentence, source.get("name") or "Fonte di conoscenza")
            for sentence in _sentences(source.get("content") or "")
        )
    return rows


def _actor_from_sentence(sentence: str) -> str:
    lowered = sentence.casefold()
    positions = []
    for role in ROLE_WORDS:
        match = re.search(rf"(?<!\w){re.escape(role)}(?!\w)", lowered)
        if match:
            positions.append((match.start(), -len(role), role))
    if positions:
        return min(positions)[2].title()
    subject = re.match(r"(?i)^(?:poi |successivamente |infine )?(?:il|la|un|una|the|an?)?\s*([A-ZÀ-ÖØ-Ý][\wÀ-ÿ -]{2,40}?)\s+", sentence)
    candidate = _clean_markdown(subject.group(1)) if subject else ""
    if candidate.casefold() in {"se", "se il", "quando", "per", "poi", "successivamente", "infine"} or ACTION_VERBS.search(candidate):
        return "Ruolo da confermare"
    return candidate or "Ruolo da confermare"


def _extract_process_steps(context: dict) -> list[dict]:
    steps = []
    seen = set()
    previous_actor = "Ruolo da confermare"
    for sentence, source in _sentence_source_rows(context):
        action = _clean_markdown(sentence)
        key = re.sub(r"\W+", " ", action.casefold()).strip()
        if not ACTION_VERBS.search(action) or key in seen:
            continue
        seen.add(key)
        actor = _actor_from_sentence(action)
        actor_origin = "knowledge"
        if actor == "Ruolo da confermare" and previous_actor != "Ruolo da confermare":
            actor = previous_actor
            actor_origin = "inferred_from_sequence"
        elif actor != "Ruolo da confermare":
            previous_actor = actor
        steps.append(
            {
                "id": f"STEP-{len(steps) + 1:02d}",
                "order": len(steps) + 1,
                "actor": actor,
                "actor_origin": actor_origin,
                "action": action[:320],
                "result": _step_result(action),
                "source": source,
                "origin": "knowledge",
            }
        )
        if len(steps) >= 14:
            break
    return steps


def _step_result(sentence: str) -> str:
    lowered = sentence.casefold()
    if any(term in lowered for term in ("aggiorna", "registra", "salva", "record", "update")):
        return "Informazioni operative aggiornate"
    if any(term in lowered for term in ("approv", "valuta", "verifica", "controlla", "review", "check")):
        return "Decisione o controllo documentato"
    if any(term in lowered for term in ("invia", "comunica", "notifica", "send", "notify")):
        return "Stakeholder aggiornato"
    if any(term in lowered for term in ("chiude", "rimbors", "sostit", "close", "refund", "replace")):
        return "Caso portato verso la risoluzione"
    return "Passaggio completato"


def _extract_named_values(text: str, vocabulary: tuple[str, ...]) -> list[str]:
    lowered = text.casefold()
    return [value for value in vocabulary if re.search(rf"(?<!\w){re.escape(value)}(?!\w)", lowered)]


def _extract_channels(text: str) -> list[str]:
    lowered = text.casefold()
    return [name for name, terms in CHANNEL_WORDS.items() if any(term in lowered for term in terms)]


def _extract_outcomes(steps: list[dict], rules: list[dict]) -> list[dict]:
    signals = re.compile(
        r"(?i)\b(?:chiud\w*|approv\w*|rifiut\w*|rimbors\w*|sostitui\w*|swap|"
        r"escal\w*|assegn\w*|complet\w*|closed?|approved?|rejected?|refund\w*|replace\w*)\b"
    )
    values = []
    for item in [*steps, *rules]:
        action = _clean_markdown(item.get("action") or item.get("statement"))
        if action and signals.search(action):
            values.append(
                {
                    "name": _rule_label(action, action),
                    "description": action[:220],
                    "origin": item.get("origin") or "knowledge",
                    "source": item.get("source") or "Operational model",
                }
            )
    return _dedupe(values, "name")[:8]


def _infer_operating_environment(company: dict, text: str) -> str:
    parts = [company.get("business_model"), company.get("industry")]
    channels = _extract_channels(text)
    if channels:
        parts.append(" / ".join(channels))
    markets = company.get("markets") or []
    if markets:
        parts.append(", ".join(markets[:4]))
    return " · ".join(str(value) for value in parts if value) or "Operating environment to be refined"


def _detect_conflicts(context: dict) -> list[dict]:
    """Flag competing values only when the surrounding topic is substantially similar."""
    candidates = []
    for sentence, source in _sentence_source_rows(context):
        values = re.findall(r"(?:€\s*)?\d+(?:[.,]\d+)?\s*(?:EUR|€|giorni|days?|mesi|months?|ore|hours?)?", sentence, flags=re.IGNORECASE)
        if not values:
            continue
        words = {
            word for word in re.findall(r"[a-zà-ÿ]{4,}", sentence.casefold())
            if word not in {"della", "delle", "sono", "deve", "entro", "oltre", "from", "with", "that", "this"}
        }
        candidates.append((sentence, source, values, words))
    conflicts = []
    for index, left in enumerate(candidates):
        for right in candidates[index + 1:]:
            shared = left[3] & right[3]
            if len(shared) < 2 or set(left[2]) == set(right[2]):
                continue
            conflicts.append(
                {
                    "issue_type": f"conflict_{_slug('_'.join(sorted(shared)[:3]), 'value')}",
                    "question": f"Abbiamo trovato due indicazioni diverse su {' / '.join(sorted(shared)[:3])}. Quale deve prevalere?",
                    "options": [f"{left[2][0]} — {left[1]}", f"{right[2][0]} — {right[1]}", "Mantieni da verificare"],
                    "details": {"evidence": [left[0][:220], right[0][:220]]},
                }
            )
    return _dedupe(conflicts, "issue_type")[:3]


def _model_assumptions(context: dict, extracted: dict, process_steps: list[dict]) -> list[dict]:
    company = context.get("company") or {}
    name = extracted["operation"]["name"]
    suggestions = []
    if not process_steps:
        suggestions.append(("workflow_sequence", f"Definire l'inizio, i passaggi e la fine di {name}."))
    if not extracted.get("required_fields"):
        suggestions.append(("minimum_input", "Definire le informazioni minime necessarie prima di proporre una decisione."))
    if not any(item.get("origin") == "knowledge" for item in extracted.get("escalations") or []):
        suggestions.append(("exception_owner", "Assegnare un responsabile per eccezioni e casi non coperti."))
    if not _extract_channels(_all_text(context)):
        suggestions.append(("intake_channel", "Confermare da quale canale entrano normalmente le richieste."))
    if len(company.get("markets") or []) > 1:
        suggestions.append(("market_scope", "Confermare se la stessa procedura vale in tutti i mercati indicati."))
    return [
        {"id": f"ASM-{index:02d}", "topic": topic, "statement": statement, "status": "needs_confirmation", "origin": "contextual_suggestion"}
        for index, (topic, statement) in enumerate(suggestions[:6], 1)
    ]


def _local_extraction(context: dict) -> dict:
    text = _all_text(context)
    name = _operation_name(context, text)
    case_types, _ = _extract_case_types(text, name)
    return {
        "operation": {
            "name": name,
            "purpose": _first_sentence(
                context["operation"].get("objective") or "",
                f"Create a consistent and reviewable path for {name.lower()}.",
            ),
        },
        "operational_domains": [
            {
                "id": _slug(name, "area_operativa"),
                "name": name,
                "description": _first_sentence(context["operation"].get("description") or "", name),
                "objective": _first_sentence(context["operation"].get("objective") or "", "Obiettivo da confermare"),
                "activities": _sentences(context["operation"].get("description") or "")[:3],
                "origin": "model_derived",
                "provenance": {"source_type": "derived", "confidence": .55, "evidence": [], "requires_confirmation": True},
                "evidence": [],
            }
        ],
        "case_types": case_types,
        "required_fields": _extract_required_fields(text),
        "rules": _extract_rules(text, context.get("knowledge_sources", [])),
        "escalations": _extract_escalations(text),
        "clarifications": [],
    }


def _parse_json(value: str) -> dict:
    value = value.strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start >= 0 and end > start:
            return json.loads(value[start:end + 1])
        raise


def public_provider_error(exc: Exception) -> tuple[str, str]:
    """Map provider failures to useful, non-sensitive messages for the public UI."""
    detail = str(exc).casefold()
    if isinstance(exc, anthropic.RateLimitError):
        return "rate_limit", "È stato raggiunto il limite temporaneo di richieste Anthropic. Attendi un minuto e riprova: le informazioni inserite sono al sicuro."
    if isinstance(exc, anthropic.AuthenticationError):
        return "authentication", "La chiave API Anthropic configurata su Render è stata rifiutata. Controlla la variabile segreta e ripubblica il servizio."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "permission", "La chiave API Anthropic non può utilizzare il modello Claude configurato. Controlla i permessi dello spazio di lavoro."
    if any(term in detail for term in ("credit balance", "credit_balance", "billing", "insufficient credit", "insufficient_quota", "purchase credits")):
        return "billing", "Anthropic ha rifiutato la chiamata perché il credito API non è disponibile o sufficiente. Aggiungi credito e riprova."
    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return "connection", "La connessione ad Anthropic è stata interrotta. Attendi un momento e riprova: le informazioni inserite sono al sicuro."
    if isinstance(exc, json.JSONDecodeError) or "output token limit" in detail:
        return "incomplete_output", "Claude ha raggiunto il limite di output prima di completare il modello operativo. Riprova con meno note o documenti."
    if isinstance(exc, ValueError):
        return "invalid_model", "Claude ha restituito un modello operativo che non ha superato la validazione. Riprova: non è stato sostituito con un risultato locale."
    if isinstance(exc, anthropic.BadRequestError):
        return "bad_request", "Anthropic ha rifiutato la richiesta. Controlla il modello configurato e le impostazioni di generazione."
    return "provider_error", "Claude non è riuscito a completare il modello operativo. Riprova tra poco."


def _normalise_extraction(payload: dict, context: dict) -> dict:
    text = _all_text(context)
    sources = context.get("knowledge_sources", [])
    operation = payload.get("operation") if isinstance(payload.get("operation"), dict) else {}
    name = _clean_markdown(operation.get("name"))[:72] or _operation_name(context, text)
    purpose = _clean_markdown(operation.get("purpose"))[:240] or _first_sentence(context["operation"].get("objective") or "", f"Create a consistent and reviewable path for {name.lower()}.")

    operational_domains = []
    for index, item in enumerate(payload.get("operational_domains") or [], 1):
        if not isinstance(item, dict):
            item = {"name": str(item)}
        label = _clean_markdown(item.get("name"))[:90]
        if not label:
            continue
        operational_domains.append(
            {
                "id": _slug(item.get("id") or label, f"domain_{index}"),
                "name": label,
                "description": _clean_markdown(item.get("description"))[:240],
                "objective": _clean_markdown(item.get("objective"))[:240],
                "activities": [_clean_markdown(value)[:160] for value in item.get("activities") or [] if _clean_markdown(value)][:5],
                "origin": item.get("origin") if item.get("origin") in {"knowledge", "model_derived", "human_review"} else "model_derived",
                "provenance": item.get("provenance") if isinstance(item.get("provenance"), dict) else {},
                "evidence": [str(value)[:220] for value in item.get("evidence") or []][:3],
            }
        )
    operational_domains = _dedupe(operational_domains, "name")[:10]
    if not operational_domains:
        operational_domains = [
            {
                "id": _slug(name, "area_operativa"), "name": name,
                "description": _first_sentence(context["operation"].get("description") or "", name),
                "objective": purpose, "activities": [], "origin": "model_derived",
                "provenance": {"source_type": "derived", "confidence": .45, "evidence": [], "requires_confirmation": True},
                "evidence": [],
            }
        ]

    case_types = []
    for index, item in enumerate(payload.get("case_types") or [], 1):
        if not isinstance(item, dict):
            item = {"name": str(item)}
        label = _clean_markdown(item.get("name"))[:80]
        if not label:
            continue
        case_types.append(
            {
                "id": _slug(item.get("id") or label, f"case_{index}"),
                "name": label,
                "description": _clean_markdown(item.get("description"))[:220] or "Operational path identified from the supplied knowledge.",
                "operation": name,
                "keywords": [str(value).casefold()[:80] for value in item.get("keywords") or []][:8],
                "origin": item.get("origin") if item.get("origin") in {"knowledge", "model_derived", "inferred_placeholder", "human_review"} else "knowledge",
                "provenance": item.get("provenance") if isinstance(item.get("provenance"), dict) else {},
                "evidence": [str(value)[:220] for value in item.get("evidence") or []][:3],
            }
        )
    case_types = _dedupe(case_types, "name")[:8]
    if not case_types:
        case_types, _ = _extract_case_types(text, name)

    fields = []
    for index, item in enumerate(payload.get("required_fields") or [], 1):
        if not isinstance(item, dict):
            item = {"label": str(item)}
        label = _clean_markdown(item.get("label"))[:90]
        if not label:
            continue
        fields.append(
            {
                "id": _slug(item.get("id") or label, f"field_{index}"),
                "label": label,
                "required": True,
                "origin": item.get("origin") if item.get("origin") in {"knowledge", "model_derived", "human_review"} else "knowledge",
                "provenance": item.get("provenance") if isinstance(item.get("provenance"), dict) else {},
                "evidence": [str(value)[:220] for value in item.get("evidence") or []][:3],
            }
        )
    fields = _dedupe(fields, "label")[:12]

    rules = []
    used_ids = set()
    for index, item in enumerate(payload.get("rules") or [], 1):
        if not isinstance(item, dict):
            item = {"statement": str(item)}
        statement = _clean_markdown(item.get("statement"))[:320]
        if not statement:
            continue
        rule_id = re.sub(r"[^A-Z0-9-]", "", str(item.get("id") or "").upper())[:16]
        if not rule_id or rule_id in used_ids:
            rule_id = f"{_rule_prefix(statement)}-{index:02d}"
        while rule_id in used_ids:
            rule_id = f"{_rule_prefix(statement)}-{index + len(used_ids):02d}"
        used_ids.add(rule_id)
        evidence = [str(value)[:220] for value in item.get("evidence") or []][:3]
        source = _clean_markdown(item.get("source"))[:120] or _source_for_excerpt(evidence[0] if evidence else statement, sources)
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", .88))))
        except (TypeError, ValueError):
            confidence = .88
        rules.append(
            {
                "id": rule_id,
                "label": _clean_markdown(item.get("label"))[:110] or statement[:96],
                "statement": statement,
                "condition": _clean_markdown(item.get("condition"))[:220] or _condition_and_action(statement)[0],
                "action": _clean_markdown(item.get("action"))[:240] or statement[:220],
                "source": source,
                "confidence": confidence,
                "status": "active",
                "origin": item.get("origin") if item.get("origin") in {"knowledge", "model_derived", "human_review"} else "knowledge",
                "provenance": item.get("provenance") if isinstance(item.get("provenance"), dict) else {},
                "evidence": evidence,
            }
        )
    if not rules:
        rules = _extract_rules(text, sources)
    if not any(re.search(r"(?i)\b(human|operator|operatore|approv)\w*\b", item["statement"]) for item in rules):
        rules.append(
            {
                "id": "SAFE-02",
                "label": "Conferma umana prima dell’esecuzione",
                "statement": "Le azioni esterne richiedono una conferma umana.",
                "condition": "Prima di eseguire qualsiasi azione esterna",
                "action": "Preparare la raccomandazione e attendere la decisione di un operatore.",
                "source": "Protezione predefinita dello spazio di lavoro",
                "confidence": 1.0,
                "status": "active",
                "origin": "safeguard",
                "evidence": [],
            }
        )

    escalations = []
    for index, item in enumerate(payload.get("escalations") or [], 1):
        if not isinstance(item, dict):
            continue
        owner = _clean_markdown(item.get("owner"))[:70]
        trigger = _clean_markdown(item.get("trigger"))[:220]
        if not owner or not trigger:
            continue
        escalations.append(
            {
                "id": f"ESC-{index:02d}",
                "trigger": trigger,
                "owner": owner,
                "action": _clean_markdown(item.get("action"))[:220] or f"Sottoporre il caso alla revisione di {owner}.",
                "origin": item.get("origin") if item.get("origin") in {"knowledge", "model_derived", "human_review"} else "knowledge",
                "provenance": item.get("provenance") if isinstance(item.get("provenance"), dict) else {},
                "evidence": [str(value)[:220] for value in item.get("evidence") or []][:3],
            }
        )
    if not escalations:
        escalations = _extract_escalations(text)

    clarifications = []
    for item in payload.get("clarifications") or []:
        if not isinstance(item, dict):
            continue
        question = _clean_markdown(item.get("question"))[:260]
        if not question:
            continue
        options = [_clean_markdown(value)[:100] for value in item.get("options") or [] if _clean_markdown(value)][:4]
        clarifications.append(
            {
                "issue_type": _slug(item.get("issue_type") or question, "clarification"),
                "question": question,
                "options": options or ["Aggiungila alla procedura", "Lasciala irrisolta"],
                "details": item.get("details") if isinstance(item.get("details"), dict) else {},
            }
        )
    return {
        "operation": {"name": name, "purpose": purpose},
        "operational_domains": operational_domains,
        "case_types": case_types,
        "required_fields": fields,
        "rules": rules[:20],
        "escalations": escalations[:4],
        "clarifications": clarifications[:6],
    }


def _generated_clarifications(context: dict, extracted: dict) -> list[dict]:
    issues = [*list(extracted.get("clarifications") or []), *_detect_conflicts(context)]
    case_types = extracted.get("case_types") or []
    fields = extracted.get("required_fields") or []
    rules = extracted.get("rules") or []
    escalations = extracted.get("escalations") or []
    semantic_types = any(item.get("origin") == "knowledge" for item in case_types)
    if not semantic_types:
        issues.append({"issue_type": "case_type_gap", "question": f"Quali tipi ricorrenti deve distinguere {extracted['operation']['name']}?", "options": ["Li aggiungo nel documento", "Un solo percorso per ora", "Da osservare sui primi casi"], "details": {}})
    if not fields:
        issues.append({"issue_type": "required_information_gap", "question": "Quali informazioni devono essere disponibili prima di prendere una decisione?", "options": ["Le aggiungo nel documento", "Nessun campo obbligatorio", "Da definire dopo i primi casi"], "details": {}})
    if not any(item.get("origin") == "knowledge" for item in escalations):
        issues.append({"issue_type": "missing_owner", "question": "Chi deve decidere sulle eccezioni o sui casi ambigui?", "options": ["Responsabile del processo", "Responsabile del gruppo", "Da assegnare caso per caso"], "details": {}})
    if len([item for item in rules if item.get("origin") == "knowledge"]) < 2:
        issues.append({"issue_type": "decision_rule_gap", "question": "Le fonti contengono poche regole decisionali esplicite. Come va trattata questa prima versione?", "options": ["Bozza da completare", "Revisione manager obbligatoria", "Aggiungeremo regole dai casi"], "details": {}})
    if len(context["company"].get("markets") or []) > 1 and not any("market" in item["statement"].casefold() for item in rules):
        issues.append({"issue_type": "market_scope", "question": "Le stesse regole valgono in tutti i mercati indicati?", "options": ["Un’unica procedura globale", "Regole diverse per mercato", "Non è ancora deciso"], "details": {"markets": context["company"]["markets"]}})
    if not context.get("knowledge_sources"):
        issues.append({"issue_type": "knowledge_gap", "question": "Il modello deriva solo dalla descrizione iniziale. Come deve essere considerato?", "options": ["Bozza utilizzabile", "Revisione obbligatoria", "Aggiungerò documenti"], "details": {}})
    return _dedupe(issues, "issue_type")[:5]


SCORE_MAX = {
    "company_context": 12,
    "operation_definition": 14,
    "knowledge_sources": 10,
    "case_types": 15,
    "required_information": 14,
    "decision_rules": 20,
    "escalation_ownership": 7,
    "clarifications": 8,
    "scenario_validation": 10,
}


def _score_rows(context: dict, extracted: dict, clarifications: list[dict]) -> dict:
    company = context["company"]
    company_values = [company.get("name"), company.get("description"), company.get("industry"), company.get("business_model"), company.get("team_size"), company.get("markets")]
    operation = context["operation"]
    operation_values = [operation.get("description"), operation.get("objective"), operation.get("current_process")]
    sources = context.get("knowledge_sources") or []
    case_types = extracted.get("case_types") or []
    fields = extracted.get("required_fields") or []
    rules = extracted.get("rules") or []
    escalations = extracted.get("escalations") or []
    verified_origins = {"knowledge", "human_review"}
    semantic_types = len([item for item in case_types if item.get("origin") in verified_origins])
    explicit_rules = len([item for item in rules if item.get("origin") in verified_origins])
    explicit_escalation = any(item.get("origin") in verified_origins for item in escalations)
    return {
        "company_context": min(12, sum(bool(value) for value in company_values) * 2),
        "operation_definition": min(14, sum(bool(value) for value in operation_values) * 5),
        "knowledge_sources": min(10, 0 if not sources else 6 + len(sources) * 2),
        "case_types": min(15, semantic_types * 4) if semantic_types else 2,
        "required_information": min(14, len([item for item in fields if item.get("origin") in verified_origins]) * 2),
        "decision_rules": min(20, explicit_rules * 3),
        "escalation_ownership": 7 if explicit_escalation else 1,
        "clarifications": 8 if not clarifications else 0,
        "scenario_validation": 0,
    }


def _score(rows: dict, *, has_open_clarifications: bool, scenarios_reviewed: bool) -> int:
    value = sum(int(item or 0) for item in rows.values())
    if not scenarios_reviewed:
        value = min(value, 88 if not has_open_clarifications else 82)
    return max(18, min(98, value))


def _breakdown(rows: dict) -> list[dict]:
    labels = {
        "company_context": "Contesto aziendale",
        "operation_definition": "Operazione e obiettivo",
        "knowledge_sources": "Copertura della conoscenza",
        "case_types": "Tipi di caso specifici",
        "required_information": "Informazioni necessarie",
        "decision_rules": "Regole decisionali sostenute dalle fonti",
        "escalation_ownership": "Responsabilità delle escalation",
        "clarifications": "Ambiguità risolte",
        "scenario_validation": "Scenari di verifica validati",
    }
    return [
        {"id": key, "label": labels[key], "earned": int(rows.get(key) or 0), "maximum": maximum}
        for key, maximum in SCORE_MAX.items()
    ]


def _assemble_model(context: dict, payload: dict, provider_name: str) -> dict:
    extracted = _normalise_extraction(payload, context)
    process_steps = _extract_process_steps(context)
    clarifications = _generated_clarifications(context, extracted)
    score_rows = _score_rows(context, extracted, clarifications)
    completeness = _score(score_rows, has_open_clarifications=bool(clarifications), scenarios_reviewed=False)
    company = context["company"]
    operation = extracted["operation"]
    sources = context.get("knowledge_sources") or []
    policies = [
        {"id": f"POL-{index:02d}", "name": source.get("name") or f"Fonte di conoscenza {index}", "source_id": source.get("id"), "status": "strutturata"}
        for index, source in enumerate(sources, 1)
    ]
    model = {
        "schema_version": "1.2",
        "provider": provider_name,
        "operation": operation,
        "playbook": {"name": f"Procedura {operation['name']}", "version": "1.0", "status": "in revisione"},
        "policies": policies,
        "company_context": {
            "summary": _first_sentence(company.get("description") or "", f"{company.get('name') or 'L’azienda'} sta configurando il suo primo flusso operativo."),
            "industry": company.get("industry"),
            "markets": company.get("markets") or [],
            "business_model": company.get("business_model"),
            "team_size": company.get("team_size"),
            "operating_environment": _infer_operating_environment(company, _all_text(context)),
        },
        "operational_domains": extracted.get("operational_domains") or [],
        "case_types": extracted["case_types"],
        "required_fields": extracted["required_fields"],
        "rules": extracted["rules"],
        "escalations": extracted["escalations"],
        "process": {
            "starts_when": process_steps[0]["action"] if process_steps else "Da confermare",
            "ends_when": process_steps[-1]["result"] if process_steps else "Da confermare",
            "steps": process_steps,
            "channels": _extract_channels(_all_text(context)),
            "systems": _extract_named_values(_all_text(context), SYSTEM_WORDS),
            "roles": _extract_named_values(_all_text(context), ROLE_WORDS),
        },
        "outcomes": _extract_outcomes(process_steps, extracted["rules"]),
        "controls": [
            {"id": "CTRL-01", "name": "Tracciabilità", "statement": "Ogni raccomandazione mantiene il riferimento alla fonte che l'ha generata.", "origin": "system_safeguard"},
            {"id": "CTRL-02", "name": "Conferma umana", "statement": "Le azioni esterne restano bloccate finché un operatore non le conferma.", "origin": "system_safeguard"},
        ],
        "assumptions": _model_assumptions(context, extracted, process_steps),
        "ambiguities": clarifications,
        "knowledge": {
            "source_count": len(sources),
            "source_names": [item.get("name") for item in sources],
            "privacy": context.get("privacy") or {},
        },
        "completeness": completeness,
        "completeness_scores": score_rows,
        "completeness_breakdown": _breakdown(score_rows),
        "remaining_setup": [item["question"] for item in clarifications][:6],
        "validation": {"reviewed": 0, "passed": 0, "adjustments": 0},
        "learning": {"decisions_can_be_promoted_to_rules": True},
    }
    ontology = payload.get("ontology") if isinstance(payload.get("ontology"), dict) else None
    if ontology:
        lifecycle = ontology.get("lifecycle") or []
        actors = ontology.get("actors") or []
        systems = ontology.get("systems") or []
        steps = []
        for stage in sorted(lifecycle, key=lambda item: item.get("order") or 999):
            actions = stage.get("actions") or []
            action = "; ".join(actions) or stage.get("name") or "Fase da revisionare"
            outputs = stage.get("outputs") or []
            provenance = stage.get("provenance") or {}
            steps.append(
                {
                    "id": stage.get("id") or f"STEP-{len(steps) + 1:02d}",
                    "order": stage.get("order") or len(steps) + 1,
                    "stage": stage.get("name"),
                    "actor": stage.get("owner") or "Responsabile da confermare",
                    "action": action,
                    "decisions": stage.get("decisions") or [],
                    "required_information": stage.get("required_information") or [],
                    "result": "; ".join(outputs) or "Output da confermare",
                    "source": "Ricostruzione operativa di Claude",
                    "origin": "knowledge" if provenance.get("source_type") == "explicit" else "model_derived",
                    "provenance": provenance,
                }
            )
        operation_detail = ontology.get("operation") or {}
        model["schema_version"] = operational_grammar.GRAMMAR_VERSION
        model["operational_grammar"] = ontology
        model["actors"] = actors
        model["systems"] = systems
        model["exceptions"] = ontology.get("exceptions") or []
        model["constraints"] = ontology.get("constraints") or []
        model["metrics"] = ontology.get("metrics") or []
        model["feedback_loops"] = ontology.get("feedback_loops") or []
        model["process"] = {
            "starts_when": operation_detail.get("trigger") or (steps[0]["action"] if steps else "Da confermare"),
            "ends_when": operation_detail.get("completion_definition") or (steps[-1]["result"] if steps else "Da confermare"),
            "scope": operation_detail.get("scope") or "Da confermare",
            "steps": steps,
            "channels": _extract_channels(_all_text(context)),
            "systems": [item.get("name") for item in systems if item.get("name")],
            "roles": [item.get("name") for item in actors if item.get("name")],
        }
        model["outcomes"] = [
            {
                "name": item.get("name"), "description": item.get("definition"),
                "origin": "knowledge" if (item.get("provenance") or {}).get("source_type") == "explicit" else "model_derived",
                "source": "Ricostruzione operativa di Claude", "provenance": item.get("provenance") or {},
            }
            for item in ontology.get("outcomes") or []
        ]
        suggested = []
        model["operational_domains"] = ontology.get("operational_domains") or model.get("operational_domains") or []
        for section in ("operational_domains", "case_types", "actors", "systems", "inputs", "lifecycle", "decision_rules", "exceptions", "escalations", "constraints", "outcomes", "metrics", "feedback_loops"):
            for item in ontology.get(section) or []:
                provenance = item.get("provenance") or {}
                if provenance.get("source_type") != "suggested":
                    continue
                statement = item.get("name") or item.get("statement") or item.get("trigger") or item.get("signal") or item.get("id")
                suggested.append({"id": f"ASM-{len(suggested) + 1:02d}", "topic": section, "statement": statement, "status": "needs_confirmation", "origin": "model_suggestion", "provenance": provenance})
        model["assumptions"] = suggested[:10]
    return {"model": model, "clarifications": clarifications}


class OperationalModelBehaviour:
    uses_external_provider = False

    def scenarios(self, model: dict) -> list[dict]:
        case_types = model.get("case_types") or []
        fields = model.get("required_fields") or []
        rules = model.get("rules") or []
        escalations = model.get("escalations") or [{"owner": "Responsabile del processo"}]

        def matching_rule(case_type: dict) -> dict:
            case_words = set(re.findall(r"\w+", (case_type.get("name") or "").casefold()))
            ranked = sorted(
                rules,
                key=lambda rule: len(case_words & set(re.findall(r"\w+", (rule.get("label") or rule.get("statement") or "").casefold()))),
                reverse=True,
            )
            return ranked[0] if ranked else {"id": "REGOLA-BOZZA", "action": "Seguire la procedura revisionata."}

        scenarios = []
        for case_type in case_types[:2]:
            rule = matching_rule(case_type)
            scenarios.append(
                {
                    "title": case_type.get("name") or "Caso operativo",
                    "input_summary": f"Un caso realistico di {case_type.get('name', 'caso operativo').lower()} contiene le informazioni richieste dalla procedura corrente.",
                    "recommendation": rule.get("action") or "Procedere secondo il percorso documentato.",
                    "rationale": f"Applica {rule.get('id', 'la regola più vicina sostenuta dalle fonti')} e richiede comunque una conferma umana.",
                }
            )
        missing_label = fields[0].get("label") if fields else "informazione necessaria"
        scenarios.append(
            {
                "title": f"Manca: {missing_label.lower()}",
                "input_summary": f"Arriva una richiesta pertinente senza {missing_label.lower()}.",
                "recommendation": f"Richiedere {missing_label.lower()} prima di decidere.",
                "rationale": "Informazioni incomplete non devono diventare decisioni operative presentate come certe.",
            }
        )
        if len(scenarios) < 3:
            scenarios.append(
                {
                    "title": "Caso non coperto dalla procedura documentata",
                    "input_summary": "Una richiesta introduce un’eccezione che la conoscenza disponibile non risolve.",
                    "recommendation": f"Sottoporre il caso a {escalations[0].get('owner', 'responsabile del processo')} per una revisione.",
                    "rationale": "Il modello mantiene una responsabilità umana invece di inventare una regola.",
                }
            )
        return scenarios[:4]

    def resolve(self, model: dict, answers: list[dict]) -> dict:
        resolved = {item.get("issue_type"): item.get("answer") for item in answers if item.get("answer")}
        updated = json.loads(json.dumps(model, ensure_ascii=False))
        updated["clarification_answers"] = resolved
        updated["ambiguities"] = [
            {**item, "resolved": item.get("issue_type") in resolved, "answer": resolved.get(item.get("issue_type"))}
            for item in model.get("ambiguities", [])
        ]
        unresolved = [item for item in updated["ambiguities"] if not item.get("resolved")]
        total = len(updated["ambiguities"])
        score_rows = {**(model.get("completeness_scores") or {})}
        score_rows["clarifications"] = round(SCORE_MAX["clarifications"] * (total - len(unresolved)) / total) if total else SCORE_MAX["clarifications"]
        updated["completeness_scores"] = score_rows
        updated["completeness_breakdown"] = _breakdown(score_rows)
        updated["remaining_setup"] = [item.get("question") for item in unresolved]
        if resolved.get("missing_owner"):
            owner = resolved["missing_owner"]
            escalations = updated.get("escalations") or []
            if escalations and escalations[0].get("origin") == "safeguard":
                escalations[0].update({"owner": owner, "action": f"Sottoporre il caso a {owner}.", "origin": "human_review"})
        if resolved.get("market_scope"):
            updated.setdefault("company_context", {})["market_rule"] = resolved["market_scope"]
        updated.setdefault("review", {})["clarifications_resolved"] = len(resolved)
        updated["completeness"] = _score(score_rows, has_open_clarifications=bool(unresolved), scenarios_reviewed=False)
        return updated

    def review(self, model: dict, edits: dict) -> dict:
        """Apply a compact human edit to the generated document and keep its audit meaning."""
        updated = json.loads(json.dumps(model, ensure_ascii=False))
        operation = edits.get("operation") if isinstance(edits.get("operation"), dict) else {}
        name = _clean_markdown(operation.get("name"))[:72]
        purpose = _clean_markdown(operation.get("purpose"))[:240]
        if len(name) < 3 or len(purpose) < 12:
            raise ValueError("Add an operation name and a clear purpose before saving the review.")
        updated["operation"] = {"name": name, "purpose": purpose}
        updated.setdefault("playbook", {})["name"] = f"Procedura {name}"
        updated["playbook"]["status"] = "human_reviewed"

        def text_items(key: str, limit: int) -> list[dict]:
            values = edits.get(key) if isinstance(edits.get(key), list) else []
            rows = []
            for index, value in enumerate(values[:limit], 1):
                label = _clean_markdown(value.get("name" if key == "case_types" else "label") if isinstance(value, dict) else value)
                if not label:
                    continue
                rows.append(
                    {
                        "id": _slug(value.get("id") or label, f"{key}_{index}") if isinstance(value, dict) else _slug(label, f"{key}_{index}"),
                        "name" if key == "case_types" else "label": label[:100],
                        "description": (_clean_markdown(value.get("description"))[:220] if isinstance(value, dict) else "") or "Confermato durante la revisione della procedura.",
                        "operation": name,
                        "required": True,
                        "keywords": [label.casefold()],
                        "origin": "human_review",
                        "evidence": ["Revisione umana della configurazione"],
                    }
                )
            return rows

        case_types = text_items("case_types", 10)
        required_fields = text_items("required_fields", 16)
        if not case_types:
            raise ValueError("Add at least one real case type handled by this operation.")
        updated["case_types"] = case_types
        updated["required_fields"] = required_fields

        rules = []
        for index, item in enumerate(edits.get("rules") or [], 1):
            if not isinstance(item, dict):
                continue
            statement = _clean_markdown(item.get("statement"))[:320]
            action = _clean_markdown(item.get("action"))[:240]
            if not statement:
                continue
            rules.append(
                {
                    "id": re.sub(r"[^A-Z0-9-]", "", str(item.get("id") or f"RULE-{index:02d}").upper())[:16] or f"RULE-{index:02d}",
                    "label": _clean_markdown(item.get("label"))[:110] or _rule_label(statement, action or statement),
                    "statement": statement,
                    "condition": statement,
                    "action": action or statement,
                    "source": "Revisione umana della configurazione",
                    "confidence": 1.0,
                    "status": "active",
                    "origin": "human_review",
                    "evidence": ["Revisione umana della configurazione"],
                }
            )
        if not rules:
            raise ValueError("Mantieni almeno una regola decisionale nella procedura.")
        updated["rules"] = rules[:24]

        escalations = []
        for index, item in enumerate(edits.get("escalations") or [], 1):
            if not isinstance(item, dict):
                continue
            owner = _clean_markdown(item.get("owner"))[:70]
            trigger = _clean_markdown(item.get("trigger"))[:220]
            if owner and trigger:
                escalations.append({"id": f"ESC-{index:02d}", "owner": owner, "trigger": trigger, "action": f"Sottoporre il caso a {owner}.", "origin": "human_review", "evidence": ["Revisione umana della configurazione"]})
        updated["escalations"] = escalations or updated.get("escalations") or []

        def reviewed_rows(key: str, field_names: tuple[str, ...], limit: int = 16) -> list[dict]:
            rows = []
            for index, item in enumerate(edits.get(key) or [], 1):
                if not isinstance(item, dict):
                    continue
                values = {field: _clean_markdown(item.get(field))[:320] for field in field_names}
                if not any(values.values()):
                    continue
                rows.append(
                    {
                        "id": f"{key.upper().replace('_', '-')[:10]}-{index:02d}",
                        **values,
                        "origin": "human_review",
                        "provenance": {
                            "source_type": "explicit",
                            "confidence": 1.0,
                            "evidence": ["Revisione umana della configurazione"],
                            "requires_confirmation": False,
                        },
                    }
                )
            return rows[:limit]

        advanced_sections = {
            "actors": ("name", "accountability"),
            "systems": ("name", "purpose"),
            "exceptions": ("owner", "trigger", "handling"),
            "constraints": ("category", "statement"),
            "metrics": ("name", "definition"),
            "feedback_loops": ("signal", "review", "improvement_action"),
        }
        for section, fields_to_keep in advanced_sections.items():
            if section in edits:
                updated[section] = reviewed_rows(section, fields_to_keep)

        steps = []
        for index, item in enumerate(edits.get("process_steps") or [], 1):
            if not isinstance(item, dict):
                continue
            action = _clean_markdown(item.get("action"))[:320]
            if action:
                steps.append({"id": f"STEP-{index:02d}", "order": index, "actor": _clean_markdown(item.get("actor"))[:70] or "Ruolo da confermare", "action": action, "result": _clean_markdown(item.get("result"))[:160] or _step_result(action), "source": "Revisione umana della configurazione", "origin": "human_review"})
        updated.setdefault("process", {})["steps"] = steps
        if steps:
            updated["process"]["starts_when"] = steps[0]["action"]
            updated["process"]["ends_when"] = steps[-1]["result"]

        ontology = updated.get("operational_grammar")
        if isinstance(ontology, dict):
            for section in advanced_sections:
                if section in edits:
                    ontology[section] = updated.get(section) or []

        score_rows = {**(updated.get("completeness_scores") or {})}
        score_rows["case_types"] = min(SCORE_MAX["case_types"], len(case_types) * 4)
        score_rows["required_information"] = min(SCORE_MAX["required_information"], len(required_fields) * 2)
        score_rows["decision_rules"] = min(SCORE_MAX["decision_rules"], len(rules) * 3)
        score_rows["escalation_ownership"] = SCORE_MAX["escalation_ownership"] if escalations else 1
        updated["completeness_scores"] = score_rows
        updated["completeness_breakdown"] = _breakdown(score_rows)
        updated["completeness"] = _score(score_rows, has_open_clarifications=any(not item.get("resolved") for item in updated.get("ambiguities") or []), scenarios_reviewed=False)
        updated["review"] = {**(updated.get("review") or {}), "model_edited": True, "source": "onboarding_manager"}
        return updated

    def apply_test_feedback(self, model: dict, scenarios: list[dict]) -> dict:
        reviewed = len([item for item in scenarios if item.get("status") in {"correct", "adjustment"}])
        passed = len([item for item in scenarios if item.get("status") == "correct"])
        adjustments = len([item for item in scenarios if item.get("status") == "adjustment"])
        total = max(len(scenarios), 1)
        score_rows = {**(model.get("completeness_scores") or {})}
        score_rows["scenario_validation"] = round(4 * reviewed / total + 6 * passed / total)
        updated = {**model}
        updated["validation"] = {"reviewed": reviewed, "passed": passed, "adjustments": adjustments}
        updated["completeness_scores"] = score_rows
        updated["completeness_breakdown"] = _breakdown(score_rows)
        updated["completeness"] = _score(
            score_rows,
            has_open_clarifications=any(not item.get("resolved") for item in model.get("ambiguities", [])),
            scenarios_reviewed=reviewed == len(scenarios) and bool(scenarios),
        )
        remaining = list(model.get("remaining_setup") or [])
        if adjustments and "Rivedi gli scenari contrassegnati come da modificare." not in remaining:
            remaining.append("Rivedi gli scenari contrassegnati come da modificare.")
        updated["remaining_setup"] = remaining[:6]
        return updated


@dataclass
class LocalOperationalModelService(OperationalModelBehaviour):
    provider_name: str = "local_evidence_extractor"

    def build(self, context: dict) -> dict:
        return _assemble_model(context, _local_extraction(context), self.provider_name)


@dataclass
class AnthropicOperationalModelService(OperationalModelBehaviour):
    provider_name: str = "anthropic_operational_model"
    uses_external_provider = True

    def build(self, context: dict) -> dict:
        api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required for the Anthropic operational model provider.")
        client = anthropic.Anthropic(api_key=api_key)
        request = dict(
            model=MODEL,
            max_tokens=MODEL_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            output_config={
                "effort": MODEL_EFFORT,
                "format": {
                    "type": "json_schema",
                    "schema": operational_grammar.schema(),
                },
            },
            messages=[
                {
                    "role": "user",
                    "content": "Reconstruct this operation from the redacted context. Return only the schema-conformant operational model.\n\n" + json.dumps(context, ensure_ascii=False),
                }
            ],
        )
        # Streaming keeps the outbound connection active on hosted environments while
        # Claude performs a long structured reconstruction.
        with client.messages.stream(**request) as stream:
            text = stream.get_final_text()
            response = stream.get_final_message()
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise ValueError("Claude reached the output token limit before returning valid JSON.")
        ontology = _parse_json(text)
        validation_errors = operational_grammar.validate(ontology)
        if validation_errors:
            raise ValueError("Invalid operational grammar: " + "; ".join(validation_errors[:6]))
        result = _assemble_model(
            context,
            operational_grammar.to_application_payload(ontology),
            self.provider_name,
        )
        usage = response.usage
        result["model"]["generation"] = {
            "model": MODEL,
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "structured_output": True,
        }
        return result


@dataclass
class ResilientOperationalModelService(OperationalModelBehaviour):
    primary: AnthropicOperationalModelService
    fallback: LocalOperationalModelService
    uses_external_provider = True

    def build(self, context: dict) -> dict:
        try:
            return self.primary.build(context)
        except (anthropic.APIError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            if (os.getenv("OPERATIONAL_MODEL_ALLOW_LOCAL_FALLBACK") or "").strip().lower() not in {"1", "true", "yes", "on"}:
                LOGGER.exception("Operational model provider failed; transparent failure enabled")
                raise
            LOGGER.warning("Operational model provider failed; explicitly using local evidence extraction: %s", exc)
            result = self.fallback.build(context)
            result["model"]["provider"] = "local_evidence_extractor_after_provider_error"
            result["model"]["remaining_setup"] = [
                *result["model"].get("remaining_setup", []),
                "Rivedi il modello estratto localmente perché il fornitore AI configurato non era disponibile.",
            ][:6]
            return result


def get_operational_model_service() -> OperationalModelBehaviour:
    """Select the provider without coupling routes or UI to an LLM vendor."""
    has_key = bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())
    configured_mode = (os.getenv("OPERATIONAL_MODEL_PROVIDER") or "").strip().lower()
    mode = configured_mode or ("anthropic" if has_key else "local")
    local = LocalOperationalModelService()
    if mode != "anthropic" or not has_key:
        return local
    primary = AnthropicOperationalModelService()
    return ResilientOperationalModelService(primary=primary, fallback=local)
