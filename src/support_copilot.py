"""Copilot operativo senza integrazioni esterne.

Il modulo rimuove gli identificatori più comuni, individua il tipo di richiesta, raccoglie solo i
fatti necessari e applica regole deterministiche prima di comporre una risposta.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import database
import domain


CATEGORY_LABELS = {
    "recesso": "Recesso",
    "doa": "Garanzia / DOA",
    "arrivato_rotto": "Arrivato danneggiato",
    "articolo_errato": "Articolo errato",
    "spedizione": "Spedizione",
    "pagamento": "Pagamento",
    "informazioni_prodotto": "Informazioni prodotto",
    "reclamo": "Reclamo",
    "altro": "Da classificare",
}

FACTS = {
    "purchase_verified": {
        "label": "Acquisto verificato",
        "question": "Hai verificato l’acquisto nel gestionale?",
        "type": "choice",
        "options": [("true", "Sì"), ("false", "Non verificabile")],
    },
    "delivery_days": {
        "label": "Giorni dalla consegna",
        "question": "Quanti giorni sono trascorsi dalla consegna?",
        "type": "number",
        "placeholder": "Es. 25",
    },
    "evidence_received": {
        "label": "Foto o video ricevuti",
        "question": "Hai ricevuto foto o video sufficienti?",
        "type": "choice",
        "options": [("true", "Sì"), ("false", "No")],
    },
    "serial_verified": {
        "label": "Seriale verificato",
        "question": "Il seriale è stato verificato?",
        "type": "choice",
        "options": [("true", "Sì"), ("false", "No")],
    },
    "product_condition": {
        "label": "Condizione prodotto",
        "question": "In quali condizioni risulta il prodotto?",
        "type": "choice",
        "options": [
            ("integral", "Integro"),
            ("opened", "Aperto / usato"),
            ("damaged", "Danneggiato"),
            ("unknown", "Non è chiaro"),
        ],
    },
    "item_matches": {
        "label": "Articolo confrontato",
        "question": "Hai confrontato articolo ricevuto e ordine?",
        "type": "choice",
        "options": [("false", "Sono diversi"), ("true", "Coincidono")],
    },
    "order_status": {
        "label": "Stato spedizione",
        "question": "Quale stato risulta nel gestionale?",
        "type": "choice",
        "options": [
            ("in_transit", "In transito"),
            ("delayed", "In ritardo"),
            ("delivered", "Consegnato"),
            ("not_found", "Non trovato"),
        ],
    },
    "payment_status": {
        "label": "Stato pagamento",
        "question": "Quale stato risulta per il pagamento?",
        "type": "choice",
        "options": [
            ("paid", "Pagato"),
            ("pending", "In attesa"),
            ("failed", "Fallito"),
            ("refunded", "Rimborsato"),
        ],
    },
    "product_identified": {
        "label": "Prodotto identificato",
        "question": "Hai identificato con certezza il prodotto?",
        "type": "choice",
        "options": [("true", "Sì"), ("false", "No")],
    },
}

REQUIRED_FACTS = {
    "doa": ["purchase_verified", "delivery_days", "evidence_received", "serial_verified"],
    "recesso": ["purchase_verified", "delivery_days", "product_condition"],
    "arrivato_rotto": ["purchase_verified", "delivery_days", "evidence_received"],
    "articolo_errato": ["purchase_verified", "item_matches"],
    "spedizione": ["purchase_verified", "order_status"],
    "pagamento": ["purchase_verified", "payment_status"],
    "informazioni_prodotto": ["product_identified"],
    "reclamo": [],
    "altro": [],
}

OUTCOME_LABELS = {
    "informazioni_richieste": "Informazioni richieste",
    "risposta_inviata": "Risposta inviata",
    "rimborso": "Rimborso",
    "swap": "Sostituzione",
    "respinto": "Richiesta respinta",
    "escalation": "Escalation",
    "risolto": "Risolto",
}


def anonymize_message(text: str) -> tuple[str, int]:
    """Rimuove gli identificatori più comuni prima della persistenza."""
    cleaned = text.strip()
    replacements = 0
    patterns = [
        (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL]"),
        (r"(?<!\w)(?:\+?39[\s.-]?)?(?:\d[\s.-]?){8,12}(?!\w)", "[TELEFONO]"),
        (r"(?i)\b(?:ordine|order)\s*(?:n[°.]?\s*)?#?\s*[A-Z0-9-]{3,}\b", "ordine [RIFERIMENTO]"),
    ]
    for pattern, replacement in patterns:
        cleaned, count = re.subn(pattern, replacement, cleaned, flags=re.IGNORECASE if "(?i)" not in pattern else 0)
        replacements += count
    return cleaned[:5000], replacements


def classify(message: str) -> dict:
    lowered = message.casefold()
    groups = [
        ("reclamo", ("avvocato", "denuncia", "chargeback", "associazione consumatori", "reclamo formale")),
        ("articolo_errato", ("articolo sbagliato", "prodotto sbagliato", "non è quello ordinato", "diverso da quello")),
        ("arrivato_rotto", ("arrivato rotto", "arrivato danneggiato", "pacco danneggiato", "rotto alla consegna")),
        ("doa", ("non funziona", "non si accende", "difettoso", "guasto", "malfunzionamento", "doa")),
        ("recesso", ("cambiato idea", "ripensamento", "restituire", "reso", "recesso")),
        ("spedizione", ("tracking", "spedizione", "consegna", "corriere", "pacco", "non è arrivato")),
        ("pagamento", ("pagamento", "addebito", "carta", "paypal", "fattura")),
        ("informazioni_prodotto", ("compatibile", "caratteristiche", "dimensioni", "come funziona", "informazioni sul prodotto")),
    ]
    for category, terms in groups:
        if any(term in lowered for term in terms):
            return {"category": category, "confidence": 0.94, "label": CATEGORY_LABELS[category]}
    return {"category": "altro", "confidence": 0.68, "label": CATEGORY_LABELS["altro"]}


def _missing(category: str, facts: dict) -> list[str]:
    return [key for key in REQUIRED_FACTS.get(category, []) if key not in facts]


def question_payload(field: str) -> dict:
    return {"id": field, **FACTS[field]}


def _base_result(category: str, facts: dict) -> dict:
    missing = _missing(category, facts)
    if missing:
        next_field = missing[0]
        return {
            "eligibility": "needs_information",
            "outcome": "raccogli_contesto",
            "rule_id": "INTAKE-01",
            "policy_sections": ["Raccolta informazioni"],
            "motivation": f"Mancano {len(missing)} fatti necessari prima di applicare la policy.",
            "next_action": FACTS[next_field]["question"],
            "draft": None,
            "missing": missing,
        }
    if facts.get("purchase_verified") is False:
        return {
            "eligibility": "needs_information",
            "outcome": "chiedi_prova_acquisto",
            "rule_id": "INTAKE-02",
            "policy_sections": ["Identificazione acquisto"],
            "motivation": "L’acquisto non è verificabile con le informazioni disponibili.",
            "next_action": "Chiedere numero d’ordine o conferma di acquisto.",
            "draft": "Ciao, per verificare la richiesta inviaci il numero d’ordine oppure l’email di conferma dell’acquisto. Appena riceveremo il riferimento potremo proseguire.",
            "missing": [],
        }
    return {}


def evaluate(category: str, facts: dict) -> dict:
    base = _base_result(category, facts)
    if base:
        return base

    if category == "doa":
        if int(facts["delivery_days"]) > 730:
            return _result("not_eligible", "rifiuta_fuori_garanzia", "GAR-01", "La segnalazione supera i 730 giorni di garanzia.", "Applicare la procedura fuori garanzia.", "Ciao, dalle informazioni disponibili l’acquisto risulta oltre il periodo di garanzia previsto. Possiamo comunque inoltrare il caso per una valutazione fuori garanzia.")
        if not facts["evidence_received"] or not facts["serial_verified"]:
            return _result("needs_information", "chiedi_foto_video", "GAR-02", "Garanzia valida, ma prove o seriale non sono ancora completi.", "Richiedere video del problema e foto del seriale.", "Ciao, per completare la verifica inviaci un breve video del malfunzionamento e una foto leggibile dell’etichetta con il seriale del prodotto.")
        return _result("eligible", "procedi_swap", "GAR-03", "Garanzia valida e prove complete.", "Prendere in carico il reso; sostituzione dopo il controllo fisico.", "Ciao, abbiamo verificato le informazioni ricevute e possiamo prendere in carico la richiesta. La sostituzione verrà confermata dopo il rientro e il controllo fisico del prodotto.")

    if category == "recesso":
        if int(facts["delivery_days"]) > 14:
            return _result("not_eligible", "rifiuta_fuori_finestra", "RET-01", "La richiesta supera i 14 giorni previsti per il recesso.", "Comunicare l’esito e verificare eventuali eccezioni.", "Ciao, dalle informazioni disponibili la richiesta risulta oltre i 14 giorni previsti per il diritto di recesso. Se ritieni che esista un’eccezione, possiamo sottoporla a revisione.")
        if facts["product_condition"] in {"opened", "damaged", "unknown"}:
            return _result("manual_review", "verifica_condizioni", "RET-02", "La condizione del prodotto richiede una valutazione dell’operatore.", "Verificare categoria, utilizzo e condizioni prima di confermare.", "Ciao, prima di confermare il recesso dobbiamo verificare le condizioni del prodotto e della confezione. Ti chiediamo di indicarci se è stato aperto o utilizzato.")
        return _result("eligible", "procedi_rimborso", "RET-03", "Richiesta entro 14 giorni e prodotto dichiarato integro.", "Prendere in carico il recesso; rimborso dopo il controllo.", "Ciao, la richiesta rientra nei termini previsti. Possiamo prendere in carico il recesso; il rimborso verrà elaborato dopo il rientro e il controllo del prodotto.")

    if category == "arrivato_rotto":
        if not facts["evidence_received"]:
            return _result("needs_information", "chiedi_foto_video", "DAM-01", "Mancano le prove del danno alla consegna.", "Richiedere foto del prodotto e dell’imballo.", "Ciao, per verificare il danno inviaci alcune foto del prodotto, dell’imballo esterno e dell’etichetta di spedizione.")
        return _result("eligible", "procedi_sostituzione", "DAM-02", "Danno alla consegna documentato.", "Aprire la pratica e sottoporre la risoluzione all’operatore.", "Ciao, abbiamo ricevuto le immagini e preso in carico la segnalazione. Un operatore verificherà ora la soluzione più adatta.")

    if category == "articolo_errato":
        if facts["item_matches"]:
            return _result("manual_review", "ricontrolla_articolo", "ORD-01", "Il confronto non conferma l’articolo errato.", "Ricontrollare SKU, variante e contenuto del pacco.", "Ciao, per completare la verifica abbiamo bisogno di una foto del prodotto ricevuto e dell’etichetta presente sulla confezione.")
        return _result("eligible", "correggi_articolo", "ORD-02", "L’articolo ricevuto non coincide con quello ordinato.", "Prendere in carico la correzione dell’ordine.", "Ciao, abbiamo verificato che l’articolo ricevuto non corrisponde a quello previsto. Prendiamo in carico la segnalazione per organizzare la soluzione corretta.")

    if category == "spedizione":
        status = facts["order_status"]
        messages = {
            "in_transit": ("needs_information", "attendi_tracking", "SHP-01", "La spedizione risulta ancora in transito.", "Comunicare lo stato e la prossima verifica.", "Ciao, la spedizione risulta ancora in transito. Continueremo a monitorarla e ti aggiorneremo se non verranno registrati nuovi movimenti."),
            "delayed": ("manual_review", "apri_verifica_corriere", "SHP-02", "La consegna risulta in ritardo.", "Aprire una verifica con il corriere.", "Ciao, la consegna risulta in ritardo. Abbiamo avviato una verifica e ti aggiorneremo non appena riceveremo nuove informazioni."),
            "delivered": ("manual_review", "verifica_consegna", "SHP-03", "Il tracking indica consegnato ma il cliente contesta la ricezione.", "Verificare prova di consegna e indirizzo.", "Ciao, il tracking indica che la spedizione è stata consegnata. Avviamo una verifica sulla prova di consegna e sull’indirizzo utilizzato."),
            "not_found": ("needs_information", "chiedi_riferimento", "SHP-04", "La spedizione non è stata identificata.", "Chiedere il riferimento dell’ordine.", "Ciao, non riusciamo ancora a identificare la spedizione. Inviaci il numero d’ordine o l’email di conferma dell’acquisto."),
        }
        return _result(*messages[status])

    if category == "pagamento":
        return _result("manual_review", "verifica_pagamento", "PAY-01", f"Lo stato del pagamento risulta: {facts['payment_status']}.", "Verificare il movimento prima di fornire conferme economiche.", "Ciao, abbiamo preso in carico la segnalazione sul pagamento. Un operatore verificherà il movimento prima di fornirti una conferma.")

    if category == "informazioni_prodotto":
        if not facts["product_identified"]:
            return _result("needs_information", "identifica_prodotto", "CAT-01", "Il prodotto non è stato identificato con certezza.", "Chiedere nome o riferimento del prodotto.", "Ciao, per darti un’informazione precisa indicaci il nome completo o il riferimento del prodotto.")
        return _result("manual_review", "consulta_scheda_prodotto", "CAT-02", "Il prodotto è identificato; la risposta deve usare la scheda approvata.", "Consultare FAQ e scheda prodotto.", "Ciao, ho identificato il prodotto. Verifico la documentazione approvata per fornirti un’informazione precisa.")

    if category == "reclamo":
        return _result("manual_review", "escalation_operatore", "ESC-01", "Il contenuto richiede gestione umana prioritaria.", "Assegnare il caso a un responsabile.", "Ciao, abbiamo preso in carico la tua segnalazione e l’abbiamo inoltrata a un responsabile per una verifica prioritaria.")

    return _result("manual_review", "classificazione_manuale", "INTAKE-03", "La richiesta non rientra nei workflow pubblicati.", "Classificare manualmente o creare una nuova regola.", "Ciao, abbiamo ricevuto la tua richiesta. Un operatore la esaminerà per fornirti una risposta corretta.")


def _result(eligibility: str, outcome: str, rule_id: str, motivation: str, next_action: str, draft: str) -> dict:
    return {
        "eligibility": eligibility,
        "outcome": outcome,
        "rule_id": rule_id,
        "policy_sections": [CATEGORY_LABELS.get(rule_id.split("-")[0].lower(), "Policy operativa")],
        "motivation": motivation,
        "next_action": next_action,
        "draft": draft,
        "missing": [],
    }


def _case_id() -> str:
    return f"CS-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def create_case(message: str, *, operator: str = "Operatore demo", path=None) -> dict:
    sanitized, redactions = anonymize_message(message)
    classification = classify(sanitized)
    category = classification["category"]
    result = evaluate(category, {})
    case = database.create_case(
        {
            "id": _case_id(),
            "session_id": f"copilot-{uuid.uuid4().hex}",
            "request_date": database.utc_now(),
            "return_type": "support_case",
            "return_reason": category,
            "detailed_reason": sanitized,
            "customer_message": sanitized,
            "ai_classification": classification,
            "confidence": classification["confidence"],
            "eligibility_result": result["eligibility"],
            "policy_applied": result["motivation"],
            "policy_decision": result,
            "suggested_resolution": result["outcome"],
            "original_suggested_response": result["draft"],
            "analysis_duration_ms": 620,
            "data_source": "Inserimento operatore",
            "source_mode": "policy_copilot",
            "source_fetched_at": database.utc_now(),
            "source_payload": {"privacy_mode": True, "redactions": redactions},
            "case_facts": {},
            "missing_information": result["missing"],
            "privacy_mode": 1,
            "assigned_operator": operator,
            "ai_mode": "policy_copilot",
        },
        path=path,
    )
    case = database.transition_case(case["id"], domain.CaseStatus.ANALYZED.value, event_type="message_analyzed", details={"category": category, "redactions": redactions}, path=path)
    if result["missing"]:
        return database.transition_case(case["id"], domain.CaseStatus.NEEDS_INFORMATION.value, event_type="context_requested", details={"missing": result["missing"]}, path=path)
    return database.transition_case(
        case["id"],
        domain.CaseStatus.WAITING_HUMAN_APPROVAL.value,
        event_type="policy_decision_ready",
        details={"rule_id": result["rule_id"], "outcome": result["outcome"]},
        path=path,
    )


def update_fact(case_id: str, field: str, raw_value, *, path=None) -> dict:
    case = database.get_case(case_id, path)
    if case is None or not str(case.get("source_mode") or "").startswith("policy_copilot"):
        raise KeyError(case_id)
    if field not in REQUIRED_FACTS.get(case["return_reason"], []):
        raise ValueError("Informazione non prevista per questo caso.")
    definition = FACTS[field]
    if definition["type"] == "number":
        try:
            value = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Inserisci un numero valido.") from exc
        if not 0 <= value <= 3650:
            raise ValueError("Il numero di giorni non è valido.")
    else:
        allowed = {item[0] for item in definition["options"]}
        value_string = str(raw_value).lower()
        if value_string not in allowed:
            raise ValueError("Valore non valido.")
        value = value_string == "true" if value_string in {"true", "false"} else value_string

    facts = {**(case.get("case_facts") or {}), field: value}
    result = evaluate(case["return_reason"], facts)
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
        event_details={"field": field, "value": value},
        path=path,
    )
    if not result["missing"] and updated["status"] == domain.CaseStatus.NEEDS_INFORMATION.value:
        updated = database.transition_case(case_id, domain.CaseStatus.WAITING_HUMAN_APPROVAL.value, event_type="policy_decision_ready", details={"rule_id": result["rule_id"], "outcome": result["outcome"]}, path=path)
    return updated


def add_customer_message(case_id: str, message: str, *, path=None) -> dict:
    case = database.get_case(case_id, path)
    if case is None or not str(case.get("source_mode") or "").startswith("policy_copilot"):
        raise KeyError(case_id)
    sanitized, redactions = anonymize_message(message)
    database.add_message(case["session_id"], "cliente", sanitized, case_id=case_id, message_type="customer_followup", metadata={"privacy_mode": True, "redactions": redactions}, path=path)
    transcript = "\n\n".join(item["message"] for item in database.get_case_messages(case_id, path) if item["role"] == "cliente")
    return database.update_case(case_id, {"customer_message": transcript, "detailed_reason": transcript}, event_type="customer_followup_received", event_details={"redactions": redactions}, path=path)


def record_outcome(case_id: str, outcome: str, response: str = "", *, modified: bool = False, reason: str = "", path=None) -> dict:
    case = database.get_case(case_id, path)
    if case is None or not str(case.get("source_mode") or "").startswith("policy_copilot"):
        raise KeyError(case_id)
    if outcome not in OUTCOME_LABELS:
        raise ValueError("Esito non valido.")
    response = response.strip()[:5000]
    if response:
        database.add_message(case["session_id"], "operatore", response, case_id=case_id, message_type="copied_response", metadata={"sent": False, "copied": True}, path=path)
    if modified or reason:
        database.add_operator_feedback(case_id, "modified_and_used" if modified else "outcome_note", reason_tag=reason or "other", original_draft=case.get("original_suggested_response"), revised_draft=response or None, path=path)
    updated = database.update_case(
        case_id,
        {
            "actual_outcome": outcome,
            "outcome_recorded_at": database.utc_now(),
            "final_response": response or case.get("final_response"),
            "human_decision": "modified_and_approved" if modified else "approved",
            "human_reason": reason or None,
        },
        event_type="actual_outcome_recorded",
        event_details={"outcome": outcome, "modified": modified},
        path=path,
    )
    if outcome == "informazioni_richieste":
        if updated["status"] == domain.CaseStatus.WAITING_HUMAN_APPROVAL.value:
            return database.transition_case(case_id, domain.CaseStatus.NEEDS_INFORMATION.value, event_type="waiting_for_customer", path=path)
        return updated
    if outcome == "escalation":
        if updated["status"] in {domain.CaseStatus.NEEDS_INFORMATION.value, domain.CaseStatus.WAITING_HUMAN_APPROVAL.value}:
            return database.transition_case(case_id, domain.CaseStatus.ESCALATED.value, event_type="case_escalated", details={"by": "operator"}, path=path)
        return updated
    if outcome in {"rimborso", "swap", "respinto", "risolto"}:
        if updated["status"] == domain.CaseStatus.WAITING_HUMAN_APPROVAL.value:
            target = domain.CaseStatus.REJECTED.value if outcome == "respinto" else domain.CaseStatus.APPROVED.value
            updated = database.transition_case(case_id, target, event_type="operator_decision_recorded", details={"outcome": outcome}, path=path)
        if updated["status"] in {domain.CaseStatus.APPROVED.value, domain.CaseStatus.REJECTED.value, domain.CaseStatus.NEEDS_INFORMATION.value, domain.CaseStatus.ESCALATED.value}:
            updated = database.transition_case(case_id, domain.CaseStatus.CLOSED.value, event_type="case_closed", details={"outcome": outcome}, path=path)
    return updated


def view_model(case: dict | None) -> dict:
    if not case:
        return {"case": None, "question": None}
    missing = case.get("missing_information") or []
    fact_rows = []
    for field, value in (case.get("case_facts") or {}).items():
        definition = FACTS.get(field, {"label": field})
        displayed = value
        if isinstance(value, bool):
            displayed = "Sì" if value else "No"
        elif definition.get("options"):
            displayed = dict(definition["options"]).get(str(value), value)
        fact_rows.append({"id": field, "label": definition["label"], "value": displayed})
    return {
        "case": case,
        "question": question_payload(missing[0]) if missing else None,
        "category_label": CATEGORY_LABELS.get(case.get("return_reason"), "Da classificare"),
        "outcome_labels": OUTCOME_LABELS,
        "fact_rows": fact_rows,
    }


DEMO_CASES = [
    {
        "slug": "copilot-demo-doa",
        "message": "Il dispositivo non si accende più. Vorrei utilizzare la garanzia.",
        "facts": {"purchase_verified": True, "delivery_days": 84, "evidence_received": True, "serial_verified": True},
        "outcome": "swap",
        "response": "Abbiamo verificato la richiesta. La sostituzione sarà confermata dopo il rientro e il controllo fisico del prodotto.",
    },
    {
        "slug": "copilot-demo-recesso",
        "message": "Ho cambiato idea e vorrei restituire il prodotto ricevuto pochi giorni fa.",
        "facts": {"purchase_verified": True, "delivery_days": 6, "product_condition": "integral"},
        "outcome": "rimborso",
    },
    {
        "slug": "copilot-demo-spedizione",
        "message": "La spedizione è ferma da molti giorni e il tracking non si aggiorna.",
        "facts": {"purchase_verified": True, "order_status": "delayed"},
        "outcome": "escalation",
    },
    {
        "slug": "copilot-demo-pagamento",
        "message": "Il pagamento con carta sembra fallito ma vedo comunque un movimento.",
        "facts": {"purchase_verified": True, "payment_status": "failed"},
        "outcome": "risposta_inviata",
        "modified": True,
        "reason": "tono_style",
    },
    {
        "slug": "copilot-demo-prodotto",
        "message": "Vorrei sapere se questo accessorio è compatibile con il mio dispositivo.",
        "facts": {"product_identified": True},
        "outcome": "risposta_inviata",
    },
    {
        "slug": "copilot-demo-errato",
        "message": "Ho ricevuto un articolo diverso da quello che avevo scelto.",
        "facts": {"purchase_verified": True, "item_matches": False},
        "outcome": "risolto",
    },
    {
        "slug": "copilot-demo-fuori-termine",
        "message": "Vorrei fare il reso perché non mi serve più il prodotto.",
        "facts": {"purchase_verified": True, "delivery_days": 25, "product_condition": "integral"},
        "outcome": "respinto",
        "modified": True,
        "reason": "policy_interpretation",
    },
    {
        "slug": "copilot-demo-danno",
        "message": "Il pacco è arrivato danneggiato e il prodotto ha un segno evidente.",
        "facts": {"purchase_verified": True, "delivery_days": 2, "evidence_received": False},
        "outcome": "informazioni_richieste",
    },
]


def ensure_demo_cases(*, path=None) -> int:
    """Crea una volta un piccolo dataset, sempre esplicitamente marcato come demo."""
    if database.get_case_by_scenario(DEMO_CASES[0]["slug"], path):
        return 0
    created = 0
    for sample in DEMO_CASES:
        case = create_case(sample["message"], operator="Operatore demo", path=path)
        case = database.update_case(
            case["id"],
            {"source_mode": "policy_copilot_demo", "scenario_slug": sample["slug"]},
            event_type="demo_case_initialized",
            path=path,
        )
        for field, value in sample["facts"].items():
            case = update_fact(case["id"], field, value, path=path)
        response = sample.get("response") or case.get("original_suggested_response") or ""
        record_outcome(
            case["id"],
            sample["outcome"],
            response,
            modified=sample.get("modified", False),
            reason=sample.get("reason", ""),
            path=path,
        )
        created += 1
    return created
