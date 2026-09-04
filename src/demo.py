"""Scenari portfolio riproducibili, basati sullo store Shopify di test.

Le pratiche create qui non chiamano servizi esterni. I dati ordine provengono
dal manifest salvato dopo il seed Shopify del 1 settembre 2026 e sono mostrati
come snapshot registrati, mai come risposta live.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import database
import domain
import rules

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "data" / "shopify_experiment_return-agent-20260901.json"
DEMO_TODAY = date(2026, 9, 2)

SCENARIOS = [
    {
        "slug": "withdrawal-approved",
        "manifest_key": "recesso_entro_4g",
        "title": "Recesso nei termini",
        "description": "Ordine consegnato da 4 giorni: policy verificata e risposta pronta.",
        "expected": "Rimborso proposto",
        "expected_outcome": "procedi_rimborso",
        "category": "recesso",
        "tone": "violet",
        "initial_state": "approval",
        "confidence": 0.97,
        "response": "Ciao Anna, la richiesta di recesso per l'ordine #1008 rientra nei termini. Prepareremo l'etichetta e comunicheremo il costo della spedizione, che sarà detratto dal rimborso. Applica l'etichetta sull'imballo esterno, non sulla confezione del prodotto. Il rimborso sarà autorizzato solo dopo ricezione e controllo.",
    },
    {
        "slug": "doa-evidence",
        "manifest_key": "difettoso_oltre_termini",
        "title": "Difetto / DOA",
        "description": "Il cliente segnala un guasto: servono prove prima della sostituzione.",
        "expected": "Richiesta foto o video",
        "expected_outcome": "chiedi_foto_video",
        "after_evidence": "procedi_swap",
        "category": "doa",
        "tone": "cyan",
        "initial_state": "needs_information",
        "confidence": 0.94,
        "response": "Ciao Davide, per verificare il difetto dell'ordine #1015 inviaci un breve video che mostri il problema e una foto dell'etichetta seriale. Appena ricevuti, rivaluteremo la pratica.",
        "response_after": "Grazie, le prove sono state registrate. Proponiamo la sostituzione con spedizione di reso a carico dell'azienda; partirà solo dopo il rientro e il controllo fisico. Un operatore deve approvare la risposta prima di creare l'etichetta.",
    },
    {
        "slug": "hygiene-rejected",
        "manifest_key": "rasoio_aperto",
        "title": "Prodotto igienico aperto",
        "description": "Il sigillo risulta aperto: la regola blocca il recesso automatico.",
        "expected": "Recesso non idoneo",
        "expected_outcome": "rifiuta_recesso_prodotto_escluso",
        "category": "recesso",
        "tone": "rose",
        "initial_state": "approval",
        "confidence": 0.98,
        "response": "Ciao Sara, non possiamo approvare automaticamente il recesso dell'ordine #1012 perché il prodotto per uso personale risulta aperto e utilizzato. La risposta resta sottoposta alla verifica finale dell'operatore.",
    },
    {
        "slug": "warehouse-check",
        "manifest_key": "danneggiato_trasporto",
        "title": "Arrivato in magazzino",
        "description": "Il tracking è concluso, ma rimborso e swap restano bloccati fino al controllo.",
        "expected": "Controllo umano obbligatorio",
        "expected_outcome": "procedi_rimborso",
        "category": "arrivato_rotto",
        "tone": "amber",
        "initial_state": "received",
        "confidence": 0.96,
        "response": "Ciao Chiara, il reso è stato autorizzato con spedizione a carico dell'azienda e l'etichetta è disponibile. Il rimborso sarà avviato solo dopo la verifica fisica del prodotto rientrato.",
    },
    {
        "slug": "manual-escalation",
        "manifest_key": "articolo_errato",
        "title": "Articolo errato",
        "description": "Ordine e dichiarazione non coincidono: il sistema evita una decisione automatica.",
        "expected": "Escalation operatore",
        "expected_outcome": "escalation_operatore",
        "category": "articolo_errato",
        "tone": "blue",
        "initial_state": "escalated",
        "confidence": 0.91,
        "response": "Verificare manualmente la discrepanza tra articolo ordinato e articolo dichiarato dal cliente prima di promettere rimborso o sostituzione.",
    },
    {
        "slug": "refund-completed",
        "manifest_key": "recesso_entro_12g",
        "title": "Workflow completato",
        "description": "Esempio end-to-end: approvazione, etichetta, rientro, ispezione e rimborso.",
        "expected": "Pratica chiusa",
        "expected_outcome": "procedi_rimborso",
        "category": "recesso",
        "tone": "green",
        "initial_state": "closed",
        "confidence": 0.96,
        "response": "Ciao Marco, il reso è stato ricevuto e validato. Il rimborso dell'ordine #1009 è stato completato.",
    },
]


def get_scenario(slug: str | None) -> dict | None:
    return next((item for item in SCENARIOS if item["slug"] == slug), None)


def _manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"shop": "store-test.myshopify.com", "created_at": None, "items": []}
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _order_for(scenario: dict, manifest: dict) -> dict:
    item = next(
        (
            row
            for row in manifest.get("items", [])
            if row.get("scenario") == scenario["manifest_key"]
        ),
        {},
    )
    if not item:
        raise RuntimeError(f"Snapshot demo mancante: {scenario['manifest_key']}")
    return item


def _source_payload(order: dict, manifest: dict) -> dict:
    return {
        "store": manifest.get("shop"),
        "order_id": order.get("order_id"),
        "order_number": order.get("order_name"),
        "customer_id": order.get("customer_id"),
        "customer": order.get("customer_name"),
        "product": order.get("product"),
        "sku": order.get("sku"),
        "financial_status": order.get("financial_status"),
        "fulfillment_status": order.get("fulfillment_status"),
        "test_order": order.get("is_test_order", True),
        "snapshot_batch": manifest.get("batch"),
    }


def _transition(case_id: str, target: str, event: str, *, path=None, details=None):
    return database.transition_case(
        case_id, target, event_type=event, details=details, path=path
    )


def _evaluate_policy(scenario: dict, order: dict, *, evidence_received=False, seal_intact=None, category=None, requested_resolution=None) -> dict:
    """Esegue anche per la demo lo stesso motore usato dall'intake live."""
    delivery_days = order.get("delivery_days")
    delivery_date = (
        (DEMO_TODAY - timedelta(days=int(delivery_days))).isoformat()
        if delivery_days is not None
        else None
    )
    order_data = {
        "numero_ordine": order.get("order_number"),
        "stato_evasione": (
            "inevaso" if order.get("fulfillment_status") == "UNFULFILLED" else "fulfilled"
        ),
        "totale_ordine": order.get("price_eur"),
        "prodotti": [{
            "titolo": order.get("product"),
            "sku": order.get("sku"),
            "quantita": 1,
            "prezzo": order.get("price_eur"),
        }],
    }
    return rules.applica_regole(
        category or scenario["category"],
        str(order.get("order_number")),
        order_data,
        {"tracking": f"MOCK{order.get('order_number')}IT", "delivered_at": delivery_date},
        oggi=DEMO_TODAY,
        prove_fornite=evidence_received,
        sigillo_integro=seal_intact,
        confidence=scenario["confidence"],
        requested_resolution=requested_resolution,
    )


def create_scenario(scenario: dict, *, path=None) -> dict:
    manifest = _manifest()
    order = _order_for(scenario, manifest)
    session_id = f"portfolio-{scenario['slug']}"
    category = scenario["category"]
    evidence_received = scenario["initial_state"] in {"received", "closed"} and category in {"doa", "arrivato_rotto"}
    seal_intact = False if scenario["slug"] == "hygiene-rejected" else None
    decision = _evaluate_policy(
        scenario,
        order,
        evidence_received=evidence_received,
        seal_intact=seal_intact,
    )
    outcome = decision["esito_proposto"]
    if outcome != scenario["expected_outcome"]:
        raise RuntimeError(
            f"Scenario {scenario['slug']} non coerente con la policy: {outcome}"
        )
    delivery_date = (
        (DEMO_TODAY - timedelta(days=int(order["delivery_days"]))).isoformat()
        if order.get("delivery_days") is not None
        else None
    )
    return_case = database.create_case(
        {
            "id": f"DEMO-{scenario['slug'].upper()}",
            "session_id": session_id,
            "shopify_order_id": order.get("order_legacy_id"),
            "shopify_order_number": order.get("order_number"),
            "customer_id": order.get("customer_id"),
            "customer_name": order.get("customer_name"),
            "customer_email": order.get("customer_email"),
            "product_name": order.get("product"),
            "sku": order.get("sku"),
            "quantity": 1,
            "delivery_date": delivery_date,
            "request_date": database.utc_now(),
            "return_type": domain.RETURN_TYPE_BY_CATEGORY.get(category, "escalation"),
            "return_reason": category,
            "detailed_reason": order.get("customer_message"),
            "customer_message": order.get("customer_message"),
            "ai_classification": {
                "categoria": category,
                "numero_ordine": order.get("order_number"),
                "confidence": scenario["confidence"],
                "mode": "recorded_demo",
            },
            "confidence": scenario["confidence"],
            "eligibility_result": domain.eligibility_for_outcome(outcome),
            "policy_applied": decision["motivazione"],
            "policy_decision": decision,
            "suggested_resolution": outcome,
            "original_suggested_response": scenario["response"],
            "analysis_duration_ms": 1180,
            "data_source": "Shopify Admin API",
            "source_mode": "recorded_fixture",
            "source_fetched_at": manifest.get("completed_at") or manifest.get("created_at"),
            "source_payload": _source_payload(order, manifest),
            "ai_mode": "recorded_demo",
            "scenario_slug": scenario["slug"],
        },
        path=path,
    )
    _transition(
        return_case["id"],
        domain.CaseStatus.ANALYZED.value,
        "ai_analysis_completed",
        path=path,
        details={"mode": "recorded_demo", "confidence": scenario["confidence"]},
    )

    state = scenario["initial_state"]
    if state == "escalated":
        _transition(
            return_case["id"],
            domain.CaseStatus.ESCALATED.value,
            "case_escalated",
            path=path,
            details={"reason": "Exception management: human review required"},
        )
        return database.get_case(return_case["id"], path)

    _transition(
        return_case["id"],
        domain.CaseStatus.WAITING_HUMAN_APPROVAL.value,
        "policy_evaluated",
        path=path,
        details={"outcome": outcome, "rule_id": decision["rule_id"]},
    )

    if state == "approval":
        return database.get_case(return_case["id"], path)

    database.update_case(
        return_case["id"],
        {"human_decision": "approved", "final_response": scenario["response"]},
        event_type="human_approval_recorded",
        event_details={"by": "demo_operator"},
        path=path,
    )
    database.add_message(
        session_id,
        "operatore",
        scenario["response"],
        case_id=return_case["id"],
        customer_id=order.get("customer_id"),
        customer_email=order.get("customer_email"),
        message_type="approved_response",
        metadata={"sent": False, "demo": True},
        path=path,
    )

    if state == "needs_information":
        _transition(
            return_case["id"],
            domain.CaseStatus.NEEDS_INFORMATION.value,
            "additional_information_requested",
            path=path,
            details={"requested": "photo_video"},
        )
        return database.get_case(return_case["id"], path)

    _transition(
        return_case["id"], domain.CaseStatus.APPROVED.value, "return_approved", path=path
    )
    database.update_case(
        return_case["id"],
        {
            "label_status": "created",
            "shipping_provider": "mock",
            "sendcloud_return_id": f"MOCK-RET-{order['order_number']}",
            "tracking_number": f"MOCK{order['order_number']}IT",
            "label_url": f"demo://labels/{order['order_number']}",
            "api_action_count": 1,
        },
        event_type="return_label_generated",
        event_details={"provider": "mock", "external_action": False},
        path=path,
    )
    _transition(
        return_case["id"], domain.CaseStatus.LABEL_CREATED.value, "label_created", path=path
    )
    _transition(
        return_case["id"],
        domain.CaseStatus.WAITING_FOR_RETURN.value,
        "waiting_for_customer_return",
        path=path,
    )
    _transition(
        return_case["id"],
        domain.CaseStatus.RETURN_IN_TRANSIT.value,
        "return_in_transit",
        path=path,
    )
    _transition(
        return_case["id"],
        domain.CaseStatus.RETURN_RECEIVED.value,
        "return_received",
        path=path,
    )
    if state == "received":
        return database.get_case(return_case["id"], path)

    _transition(
        return_case["id"],
        domain.CaseStatus.RETURN_VALIDATED.value,
        "physical_inspection_passed",
        path=path,
        details={"by": "warehouse_operator", "note": "Seriale e accessori verificati"},
    )
    database.update_case(
        return_case["id"], {"refund_status": "pending"}, path=path
    )
    _transition(
        return_case["id"], domain.CaseStatus.REFUND_PENDING.value, "refund_started", path=path
    )
    database.update_case(
        return_case["id"], {"refund_status": "completed"}, path=path
    )
    _transition(
        return_case["id"], domain.CaseStatus.REFUNDED.value, "refund_completed", path=path
    )
    return _transition(
        return_case["id"], domain.CaseStatus.CLOSED.value, "case_closed", path=path
    )


def ensure_showcase(*, path=None) -> list[dict]:
    cases = []
    for scenario in SCENARIOS:
        existing = database.get_case_by_scenario(scenario["slug"], path)
        if existing and (existing.get("policy_decision") or {}).get(
            "policy_version"
        ) != rules.POLICY["version"]:
            database.delete_case(existing["id"], path)
            existing = None
        cases.append(existing or create_scenario(scenario, path=path))
    return cases


def reset_showcase(*, path=None) -> list[dict]:
    database.clear_demo_cases(path)
    return ensure_showcase(path=path)


def reevaluate_recorded_case(
    return_case: dict, *, evidence_received: bool = False, seal_intact=None,
    category_override: str | None = None, requested_resolution: str | None = None,
    path=None
) -> dict:
    """Rivaluta uno scenario demo senza Shopify o Claude live."""
    scenario = get_scenario(return_case.get("scenario_slug"))
    if scenario is None:
        raise ValueError("Scenario demo non riconosciuto.")
    if not evidence_received and seal_intact is None and requested_resolution is None:
        raise ValueError("Nessun nuovo elemento per la rivalutazione.")
    manifest = _manifest()
    order = _order_for(scenario, manifest)
    decision = _evaluate_policy(
        scenario,
        order,
        evidence_received=evidence_received or requested_resolution is not None,
        seal_intact=seal_intact,
        category=category_override or return_case["return_reason"],
        requested_resolution=requested_resolution,
    )
    outcome = decision["esito_proposto"]
    response = (
        scenario.get("response_after") or scenario["response"]
        if evidence_received or requested_resolution is not None
        else scenario["response"]
    )
    database.update_case(
        return_case["id"],
        {
            "eligibility_result": domain.eligibility_for_outcome(outcome),
            "return_reason": category_override or return_case["return_reason"],
            "return_type": domain.RETURN_TYPE_BY_CATEGORY.get(
                category_override or return_case["return_reason"], "escalation"
            ),
            "policy_applied": decision["motivazione"],
            "policy_decision": decision,
            "suggested_resolution": outcome,
            "original_suggested_response": response,
            "final_response": None,
            "human_decision": None,
            "human_reason": None,
        },
        event_type="case_reevaluated",
        event_details={
            "outcome": outcome,
            "rule_id": decision["rule_id"],
            "mode": "recorded_demo",
        },
        path=path,
    )
    return database.transition_case(
        return_case["id"],
        domain.CaseStatus.WAITING_HUMAN_APPROVAL.value,
        event_type="waiting_human_approval",
        details={"outcome": outcome},
        path=path,
    )
