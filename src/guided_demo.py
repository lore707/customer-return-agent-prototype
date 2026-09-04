"""Due demo guidate end-to-end basate sul workflow operativo reale.

Le demo usano dati sintetici e provider mock, ma fanno avanzare una vera
ReturnCase attraverso la state machine e registrano ogni passaggio nell'audit
trail. In questo modo il percorso guidato resta verificabile nel Workbench e
nel Database senza eseguire azioni esterne.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import database
import domain
import return_shipping
import rules

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = ROOT / "data" / "shopify_experiment_return-agent-20260901.json"
DEMO_TODAY = date(2026, 9, 4)


GUIDED_SCENARIOS = {
    "doa": {
        "slug": "doa",
        "case_id": "GUIDED-DOA",
        "scenario_slug": "guided-doa",
        "manifest_key": "difettoso_oltre_termini",
        "category": "doa",
        "eyebrow": "Caso 01 · DOA / Garanzia",
        "title": "Dal problema alla sostituzione",
        "description": "Un prodotto smette di funzionare. Il sistema verifica cliente, garanzia e prove, poi coordina rientro, controllo e swap.",
        "resolution": "Swap",
        "lookup_method": "Email di conferma acquisto",
        "customer_history": {
            "orders_total": 5,
            "returns_total": 1,
            "swaps_total": 1,
            "same_sku_swaps": 0,
            "total_spent": "€ 684,50",
            "assessment": "Storico regolare",
            "note": "Una sostituzione precedente su un altro prodotto; nessuna anomalia sullo SKU attuale.",
        },
        "evidence": {
            "files": [
                {"name": "video_malfunzionamento.mp4", "type": "Video", "status": "Verificato"},
                {"name": "foto_seriale.jpg", "type": "Foto", "status": "Verificata"},
            ],
            "assessment": "Problema visibile e seriale coerente",
            "reviewed_by": "Operatore demo",
        },
        "draft": "Ciao Davide, per verificare il problema inviaci un breve video del malfunzionamento e una foto dell’etichetta con il seriale del dispositivo.",
        "approved_response": "Ciao Davide, abbiamo verificato le prove e preso in carico la richiesta. Ti inviamo l’etichetta di reso; la sostituzione partirà dopo il rientro e il controllo del dispositivo.",
        "steps": [
            {"actor": "Cliente", "system": "Conversazione", "title": "Il cliente segnala il problema", "description": "L’aspirapolvere non si accende più e il cliente fornisce l’email della conferma d’acquisto.", "proof": "Richiesta acquisita", "action": "initial"},
            {"actor": "Shopify", "system": "Customer lookup", "title": "Cliente e ordine identificati", "description": "La ricerca tramite email collega il cliente all’ordine #1015 e al prodotto corretto.", "proof": "Ordine #1015", "action": "identity_resolved"},
            {"actor": "Shopify", "system": "Customer 360", "title": "Storico cliente verificato", "description": "Vengono controllati ordini, resi e sostituzioni precedenti, compresi quelli dello stesso SKU.", "proof": "5 ordini · storico regolare", "action": "history_loaded"},
            {"actor": "Policy engine", "system": "Garanzia", "title": "Garanzia valida", "description": "Il prodotto è stato consegnato 25 giorni fa: rientra nei 730 giorni di garanzia.", "proof": "25 / 730 giorni", "action": "eligibility_checked"},
            {"actor": "Operatore", "system": "Human gate", "title": "Richieste foto e video", "description": "La risposta preparata dall’AI viene approvata e chiede video del problema e foto del seriale.", "proof": "Prove obbligatorie", "action": "evidence_requested"},
            {"actor": "Operatore", "system": "Evidence center", "title": "Prove ricevute e valutate", "description": "Il video mostra il difetto e il seriale coincide con il prodotto acquistato.", "proof": "2 allegati verificati", "action": "evidence_reviewed"},
            {"actor": "Operatore", "system": "Human gate", "title": "Reso preso in carico", "description": "L’operatore approva la risposta finale. La pratica viene aperta con risoluzione proposta: swap.", "proof": "Approvazione umana", "action": "human_approved"},
            {"actor": "Sendcloud + Shopify", "system": "Reverse logistics", "title": "Etichetta e tracking creati", "description": "Sendcloud genera l’etichetta mock e il tracking viene registrato nello snapshot Shopify.", "proof": "Etichetta creata", "action": "label_created"},
            {"actor": "Corriere", "system": "Tracking", "title": "Il cliente spedisce il prodotto", "description": "Il tracking mock rileva la presa in carico e aggiorna automaticamente la pratica.", "proof": "Reso in transito", "action": "in_transit"},
            {"actor": "Magazzino", "system": "Inbound", "title": "Il prodotto arriva in sede", "description": "L’arrivo viene registrato, ma swap e rimborso restano bloccati fino al test fisico.", "proof": "Arrivato · da testare", "action": "received"},
            {"actor": "Operatore", "system": "Inspection", "title": "DOA confermato dal test", "description": "Condizioni, seriale, accessori e problema dichiarato vengono verificati fisicamente.", "proof": "Controllo superato", "action": "inspected"},
            {"actor": "Shopify", "system": "Return processing", "title": "Reso chiuso senza rimborso", "description": "Il reso viene segnato come elaborato successivamente: nessun rimborso viene emesso.", "proof": "Rimborso €0", "action": "shopify_deferred"},
            {"actor": "Shopify", "system": "Replacement order", "title": "Ordine sostitutivo creato", "description": "L’ordine originale viene duplicato per spedire lo stesso dispositivo al cliente.", "proof": "Nuovo ordine #1051", "action": "replacement_created"},
            {"actor": "Make", "system": "Automation", "title": "Automazione logistica avviata", "description": "Make riceve il nuovo ordine e lo inoltra alla sede logistica senza intervento manuale.", "proof": "Scenario eseguito", "action": "make_triggered"},
            {"actor": "Logistica", "system": "Fulfillment", "title": "Swap completato", "description": "La logistica prende in carico il sostitutivo e il database chiude la pratica con esito Swap.", "proof": "Chiuso · Swap", "action": "complete_swap"},
        ],
    },
    "recesso": {
        "slug": "recesso",
        "case_id": "GUIDED-RECESSO",
        "scenario_slug": "guided-recesso",
        "manifest_key": "recesso_entro_4g",
        "category": "recesso",
        "eyebrow": "Caso 02 · Diritto di recesso",
        "title": "Dalla richiesta al rimborso",
        "description": "Un cliente cambia idea entro 14 giorni. Il sistema verifica l’idoneità, coordina il rientro e abilita il rimborso solo dopo il controllo.",
        "resolution": "Rimborso",
        "lookup_method": "Numero d’ordine",
        "customer_history": {
            "orders_total": 2,
            "returns_total": 0,
            "swaps_total": 0,
            "same_sku_swaps": 0,
            "total_spent": "€ 239,80",
            "assessment": "Nessun reso precedente",
            "note": "Cliente identificato e ordine pagato; nessun’altra pratica aperta.",
        },
        "evidence": {
            "files": [],
            "assessment": "Foto/video non richiesti per il ripensamento",
            "reviewed_by": "Policy engine",
        },
        "draft": "Ciao Anna, la richiesta per l’ordine #1008 rientra nei 14 giorni previsti. Il rimborso verrà elaborato dopo il rientro e il controllo del prodotto.",
        "approved_response": "Ciao Anna, abbiamo preso in carico il recesso dell’ordine #1008. Ti inviamo l’etichetta da applicare sull’imballo esterno. Il costo di €7,90 sarà detratto dal rimborso.",
        "steps": [
            {"actor": "Cliente", "system": "Conversazione", "title": "Il cliente chiede il recesso", "description": "Il prodotto funziona, ma il cliente ha cambiato idea e comunica il numero d’ordine.", "proof": "Richiesta acquisita", "action": "initial"},
            {"actor": "Shopify", "system": "Order lookup", "title": "Ordine identificato", "description": "Shopify collega l’ordine #1008 al cliente e recupera prodotto, pagamento e consegna.", "proof": "Ordine #1008", "action": "identity_resolved"},
            {"actor": "Shopify", "system": "Customer 360", "title": "Storico cliente verificato", "description": "Il sistema controlla ordini, resi e sostituzioni precedenti prima della decisione.", "proof": "2 ordini · 0 resi", "action": "history_loaded"},
            {"actor": "Policy engine", "system": "Recesso", "title": "Richiesta entro i termini", "description": "Sono trascorsi 4 giorni dalla consegna: il recesso rientra nella finestra di 14 giorni.", "proof": "4 / 14 giorni", "action": "eligibility_checked"},
            {"actor": "AI", "system": "Draft", "title": "Risposta pronta per la revisione", "description": "L’AI prepara la risposta, ma non può inviarla né autorizzare il rimborso.", "proof": "Bozza generata", "action": "draft_ready"},
            {"actor": "Operatore", "system": "Human gate", "title": "Reso preso in carico", "description": "L’operatore approva la risposta e conferma al cliente condizioni e costo dell’etichetta.", "proof": "Approvazione umana", "action": "human_approved"},
            {"actor": "Sendcloud + Shopify", "system": "Reverse logistics", "title": "Etichetta e tracking creati", "description": "L’etichetta mock viene creata e il tracking viene associato all’ordine Shopify.", "proof": "Costo €7,90", "action": "label_created"},
            {"actor": "Corriere", "system": "Tracking", "title": "Il cliente spedisce il prodotto", "description": "Il tracking mock registra la presa in carico del pacco di reso.", "proof": "Reso in transito", "action": "in_transit"},
            {"actor": "Magazzino", "system": "Inbound", "title": "Il prodotto arriva in sede", "description": "L’arrivo non autorizza ancora il rimborso: il prodotto deve essere controllato.", "proof": "Arrivato · da controllare", "action": "received"},
            {"actor": "Operatore", "system": "Inspection", "title": "Condizioni del reso verificate", "description": "Prodotto, accessori, seriale e confezione risultano integri e rivendibili.", "proof": "Reso conforme", "action": "inspected"},
            {"actor": "Shopify", "system": "Refund", "title": "Rimborso predisposto", "description": "Il reso viene chiuso e il rimborso viene preparato al netto del costo di spedizione.", "proof": "€149,90 − €7,90", "action": "refund_pending"},
            {"actor": "Shopify", "system": "Refund", "title": "Rimborso completato", "description": "Il rimborso mock di €142,00 viene registrato e il database chiude la pratica.", "proof": "Chiuso · Rimborso", "action": "complete_refund"},
        ],
    },
}


def get_scenario(slug: str) -> dict | None:
    return GUIDED_SCENARIOS.get(slug)


def _manifest_order(manifest_key: str) -> tuple[dict, dict]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    order = next(
        (item for item in manifest.get("items", []) if item.get("scenario") == manifest_key),
        None,
    )
    if order is None:
        raise RuntimeError(f"Ordine demo non trovato: {manifest_key}")
    return manifest, order


def _delivery_date(order: dict) -> str:
    return (DEMO_TODAY - timedelta(days=int(order["delivery_days"]))).isoformat()


def _order_data(order: dict) -> dict:
    return {
        "numero_ordine": str(order["order_number"]),
        "stato_evasione": "fulfilled",
        "totale_ordine": order.get("price_eur"),
        "prodotti": [{
            "titolo": order.get("product"),
            "sku": order.get("sku"),
            "quantita": 1,
            "prezzo": order.get("price_eur"),
        }],
    }


def _decision(scenario: dict, order: dict, *, evidence_received: bool = False) -> dict:
    return rules.applica_regole(
        scenario["category"],
        str(order["order_number"]),
        _order_data(order),
        {
            "tracking": f"MOCK{order['order_number']}IT",
            "delivered_at": _delivery_date(order),
        },
        oggi=DEMO_TODAY,
        prove_fornite=evidence_received,
        confidence=0.97,
    )


def _integration_state() -> dict:
    return {
        "shopify": {"label": "Shopify", "status": "pending", "detail": "In attesa"},
        "database": {"label": "Database", "status": "complete", "detail": "Pratica creata"},
        "sendcloud": {"label": "Sendcloud", "status": "pending", "detail": "In attesa"},
        "make": {"label": "Make", "status": "pending", "detail": "In attesa"},
        "logistics": {"label": "Logistica", "status": "pending", "detail": "In attesa"},
    }


def _merge_integrations(return_case: dict, **changes: dict) -> dict:
    current = dict(return_case.get("integration_state") or _integration_state())
    for key, value in changes.items():
        current[key] = {**current.get(key, {}), **value}
    return current


def start(slug: str, *, path=None) -> dict:
    """Ripristina e avvia una pratica guidata senza chiamate esterne."""
    scenario = get_scenario(slug)
    if scenario is None:
        raise KeyError(slug)
    manifest, order = _manifest_order(scenario["manifest_key"])
    database.delete_case(scenario["case_id"], path)
    return_case = database.create_case(
        {
            "id": scenario["case_id"],
            "session_id": f"guided-{slug}",
            "shopify_order_id": order.get("order_legacy_id"),
            "shopify_order_number": str(order.get("order_number")),
            "customer_id": order.get("customer_id"),
            "customer_name": order.get("customer_name"),
            "customer_email": order.get("customer_email"),
            "product_name": order.get("product"),
            "sku": order.get("sku"),
            "quantity": 1,
            "delivery_date": _delivery_date(order),
            "request_date": database.utc_now(),
            "return_type": domain.RETURN_TYPE_BY_CATEGORY[scenario["category"]],
            "return_reason": scenario["category"],
            "detailed_reason": order.get("customer_message"),
            "customer_message": order.get("customer_message"),
            "ai_classification": {},
            "confidence": 0.97,
            "eligibility_result": "pending",
            "policy_decision": {},
            "suggested_resolution": "pending_analysis",
            "original_suggested_response": scenario["draft"],
            "analysis_duration_ms": 940,
            "data_source": "Shopify demo dataset",
            "source_mode": "recorded_fixture",
            "source_fetched_at": manifest.get("completed_at") or manifest.get("created_at"),
            "source_payload": {
                "store": manifest.get("shop"),
                "order_id": order.get("order_id"),
                "order_number": order.get("order_name"),
                "customer_id": order.get("customer_id"),
                "customer": order.get("customer_name"),
                "customer_email": order.get("customer_email"),
                "product": order.get("product"),
                "sku": order.get("sku"),
                "price_eur": order.get("price_eur"),
                "delivery_days": order.get("delivery_days"),
                "lookup_method": scenario["lookup_method"],
                "test_order": True,
            },
            "customer_history": {},
            "evidence": {},
            "integration_state": _integration_state(),
            "guided_step": 0,
            "ai_mode": "recorded_demo",
            "scenario_slug": scenario["scenario_slug"],
            "assigned_operator": "Operatore demo",
        },
        path=path,
    )
    return return_case


def _record_operator_message(return_case: dict, text: str, message_type: str, *, path=None) -> None:
    database.add_message(
        return_case["session_id"],
        "operatore",
        text,
        case_id=return_case["id"],
        customer_id=return_case.get("customer_id"),
        customer_email=return_case.get("customer_email"),
        message_type=message_type,
        metadata={"sent": True, "delivery_mode": "guided_demo"},
        path=path,
    )


def _apply_step(return_case: dict, scenario: dict, step: dict, *, path=None) -> dict:
    case_id = return_case["id"]
    action = step["action"]
    _, order = _manifest_order(scenario["manifest_key"])

    if action == "identity_resolved":
        integrations = _merge_integrations(
            return_case,
            shopify={"status": "running", "detail": "Cliente e ordine trovati"},
        )
        return database.update_case(
            case_id,
            {"integration_state": integrations},
            event_type="shopify_customer_order_resolved",
            event_details={"lookup_method": scenario["lookup_method"], "order": order["order_number"]},
            path=path,
        )

    if action == "history_loaded":
        integrations = _merge_integrations(
            return_case,
            shopify={"status": "complete", "detail": "Ordine e storico verificati"},
        )
        return database.update_case(
            case_id,
            {"customer_history": scenario["customer_history"], "integration_state": integrations},
            event_type="customer_history_reviewed",
            event_details=scenario["customer_history"],
            path=path,
        )

    if action == "eligibility_checked":
        decision = _decision(scenario, order)
        database.update_case(
            case_id,
            {
                "ai_classification": {"categoria": scenario["category"], "numero_ordine": str(order["order_number"]), "confidence": 0.97},
                "eligibility_result": domain.eligibility_for_outcome(decision["esito_proposto"]),
                "policy_applied": decision["motivazione"],
                "policy_decision": decision,
                "suggested_resolution": decision["esito_proposto"],
            },
            path=path,
        )
        return database.transition_case(
            case_id,
            domain.CaseStatus.ANALYZED.value,
            event_type="ai_analysis_completed",
            details={"rule_id": decision.get("rule_id"), "policy_version": decision.get("policy_version")},
            path=path,
        )

    if action == "evidence_requested":
        _record_operator_message(return_case, scenario["draft"], "evidence_request", path=path)
        return database.transition_case(
            case_id,
            domain.CaseStatus.NEEDS_INFORMATION.value,
            event_type="photo_video_requested",
            details={"required": ["photo", "video", "serial"]},
            path=path,
        )

    if action == "evidence_reviewed":
        decision = _decision(scenario, order, evidence_received=True)
        database.update_case(
            case_id,
            {
                "evidence": scenario["evidence"],
                "eligibility_result": domain.eligibility_for_outcome(decision["esito_proposto"]),
                "policy_applied": decision["motivazione"],
                "policy_decision": decision,
                "suggested_resolution": decision["esito_proposto"],
                "original_suggested_response": scenario["approved_response"],
            },
            event_type="evidence_reviewed",
            event_details={"result": "sufficient", "files": len(scenario["evidence"]["files"])},
            path=path,
        )
        return database.transition_case(
            case_id,
            domain.CaseStatus.WAITING_HUMAN_APPROVAL.value,
            event_type="policy_evaluated",
            details={"outcome": decision["esito_proposto"], "rule_id": decision.get("rule_id")},
            path=path,
        )

    if action == "draft_ready":
        return database.transition_case(
            case_id,
            domain.CaseStatus.WAITING_HUMAN_APPROVAL.value,
            event_type="policy_evaluated",
            details={"outcome": return_case["suggested_resolution"], "human_gate": True},
            path=path,
        )

    if action == "human_approved":
        _record_operator_message(return_case, scenario["approved_response"], "approved_response", path=path)
        database.update_case(
            case_id,
            {
                "human_decision": "approved",
                "final_response": scenario["approved_response"],
                "manual_step_count": (return_case.get("manual_step_count") or 0) + 1,
            },
            event_type="human_approval_recorded",
            event_details={"by": "Operatore demo", "sent": "simulated"},
            path=path,
        )
        return database.transition_case(
            case_id,
            domain.CaseStatus.APPROVED.value,
            event_type="return_case_opened",
            details={"resolution": scenario["resolution"].lower()},
            path=path,
        )

    if action == "label_created":
        shipment = return_shipping.get_provider().create_return(return_case)
        integrations = _merge_integrations(
            return_case,
            sendcloud={"status": "complete", "detail": "Etichetta mock creata"},
            shopify={"status": "complete", "detail": "Tracking associato all’ordine"},
        )
        database.update_case(
            case_id,
            {
                "label_status": "created",
                "shipping_provider": shipment["provider"],
                "sendcloud_return_id": shipment["return_id"],
                "tracking_number": shipment["tracking_number"],
                "label_url": shipment["label_url"],
                "integration_state": integrations,
                "api_action_count": (return_case.get("api_action_count") or 0) + 2,
            },
            event_type="return_label_generated",
            event_details={**shipment, "shopify_tracking_attached": True, "external_action": False},
            path=path,
        )
        database.transition_case(case_id, domain.CaseStatus.LABEL_CREATED.value, event_type="label_created", path=path)
        return database.transition_case(
            case_id,
            domain.CaseStatus.WAITING_FOR_RETURN.value,
            event_type="waiting_for_customer_return",
            details={"customer_notified": "simulated"},
            path=path,
        )

    if action == "in_transit":
        return database.transition_case(
            case_id,
            domain.CaseStatus.RETURN_IN_TRANSIT.value,
            event_type="carrier_tracking_updated",
            details={"status": "in_transit", "source": "mock_webhook"},
            path=path,
        )

    if action == "received":
        integrations = _merge_integrations(
            return_case,
            logistics={"status": "running", "detail": "Arrivato · da controllare"},
        )
        database.update_case(case_id, {"integration_state": integrations}, path=path)
        return database.transition_case(
            case_id,
            domain.CaseStatus.RETURN_RECEIVED.value,
            event_type="warehouse_return_received",
            details={"physical_validation_required": True},
            path=path,
        )

    if action == "inspected":
        evidence = dict(return_case.get("evidence") or {})
        evidence["physical_inspection"] = {
            "product_condition": True,
            "serial_and_accessories": True,
            "product_packaging": True,
            "declared_issue_confirmed": scenario["category"] == "doa",
        }
        database.update_case(case_id, {"evidence": evidence}, path=path)
        return database.transition_case(
            case_id,
            domain.CaseStatus.RETURN_VALIDATED.value,
            event_type="physical_inspection_passed",
            details=evidence["physical_inspection"],
            path=path,
        )

    if action == "shopify_deferred":
        integrations = _merge_integrations(
            return_case,
            shopify={"status": "complete", "detail": "Reso chiuso · nessun rimborso"},
        )
        return database.update_case(
            case_id,
            {"integration_state": integrations, "api_action_count": (return_case.get("api_action_count") or 0) + 1},
            event_type="shopify_return_closed_deferred",
            event_details={"refund_created": False, "processing_mode": "process_later", "external_action": False},
            path=path,
        )

    if action == "replacement_created":
        database.update_case(
            case_id,
            {"replacement_status": "pending", "replacement_order_number": "1051", "api_action_count": (return_case.get("api_action_count") or 0) + 1},
            event_type="shopify_replacement_order_created",
            event_details={"source_order": return_case["shopify_order_number"], "replacement_order": "1051", "external_action": False},
            path=path,
        )
        return database.transition_case(
            case_id,
            domain.CaseStatus.REPLACEMENT_PENDING.value,
            event_type="replacement_started",
            details={"replacement_order": "1051"},
            path=path,
        )

    if action == "make_triggered":
        integrations = _merge_integrations(
            return_case,
            make={"status": "complete", "detail": "Scenario inviato alla logistica"},
            logistics={"status": "running", "detail": "Ordine #1051 ricevuto"},
        )
        return database.update_case(
            case_id,
            {"integration_state": integrations, "api_action_count": (return_case.get("api_action_count") or 0) + 1},
            event_type="make_fulfillment_triggered",
            event_details={"scenario": "replacement_to_logistics", "retry_required": False, "external_action": False},
            path=path,
        )

    if action == "complete_swap":
        integrations = _merge_integrations(
            return_case,
            logistics={"status": "complete", "detail": "Sostitutivo preso in carico"},
            database={"status": "complete", "detail": "Chiuso · Swap"},
        )
        database.update_case(
            case_id,
            {"replacement_status": "completed", "integration_state": integrations},
            path=path,
        )
        database.transition_case(case_id, domain.CaseStatus.REPLACED.value, event_type="replacement_completed", path=path)
        return database.transition_case(
            case_id,
            domain.CaseStatus.CLOSED.value,
            event_type="case_closed",
            details={"resolution": "swap", "replacement_order": "1051"},
            path=path,
        )

    if action == "refund_pending":
        integrations = _merge_integrations(
            return_case,
            shopify={"status": "running", "detail": "Rimborso €142,00 predisposto"},
            make={"status": "not_required", "detail": "Non richiesto per recesso"},
        )
        database.update_case(
            case_id,
            {"refund_status": "pending", "integration_state": integrations, "api_action_count": (return_case.get("api_action_count") or 0) + 1},
            path=path,
        )
        return database.transition_case(
            case_id,
            domain.CaseStatus.REFUND_PENDING.value,
            event_type="shopify_refund_prepared",
            details={"gross": "149.90", "return_shipping": "7.90", "net": "142.00", "external_action": False},
            path=path,
        )

    if action == "complete_refund":
        integrations = _merge_integrations(
            return_case,
            shopify={"status": "complete", "detail": "Rimborso mock €142,00"},
            logistics={"status": "complete", "detail": "Prodotto rientrato"},
            database={"status": "complete", "detail": "Chiuso · Rimborso"},
        )
        database.update_case(
            case_id,
            {"refund_status": "completed", "integration_state": integrations},
            path=path,
        )
        database.transition_case(case_id, domain.CaseStatus.REFUNDED.value, event_type="refund_completed", path=path)
        return database.transition_case(
            case_id,
            domain.CaseStatus.CLOSED.value,
            event_type="case_closed",
            details={"resolution": "refund", "amount": "142.00"},
            path=path,
        )

    return return_case


def advance(case_id: str, *, path=None) -> dict:
    return_case = database.get_case(case_id, path)
    if return_case is None:
        raise KeyError(case_id)
    slug = str(return_case.get("scenario_slug") or "").removeprefix("guided-")
    scenario = get_scenario(slug)
    if scenario is None or scenario["case_id"] != case_id:
        raise ValueError("La pratica non appartiene a una demo guidata.")
    current_index = int(return_case.get("guided_step") or 0)
    next_index = current_index + 1
    if next_index >= len(scenario["steps"]):
        return return_case
    updated = _apply_step(return_case, scenario, scenario["steps"][next_index], path=path)
    return database.update_case(updated["id"], {"guided_step": next_index}, path=path)


def payload(return_case: dict) -> dict:
    """Serializza lo stato minimo usato dalla pagina guidata."""
    slug = str(return_case.get("scenario_slug") or "").removeprefix("guided-")
    scenario = get_scenario(slug)
    if scenario is None:
        raise ValueError("Scenario guidato non riconosciuto.")
    current_index = int(return_case.get("guided_step") or 0)
    step = scenario["steps"][current_index]
    history = return_case.get("customer_history") or {}
    evidence = return_case.get("evidence") or {}
    return {
        "case_id": return_case["id"],
        "scenario": slug,
        "step_index": current_index,
        "step_number": current_index + 1,
        "step_total": len(scenario["steps"]),
        "progress": round((current_index + 1) * 100 / len(scenario["steps"])),
        "completed": current_index == len(scenario["steps"]) - 1,
        "step": step,
        "case": {
            "status": return_case["status"],
            "status_label": domain.STATUS_LABELS.get(return_case["status"], return_case["status"]),
            "order_number": return_case.get("shopify_order_number"),
            "customer_name": return_case.get("customer_name"),
            "customer_email": return_case.get("customer_email"),
            "product_name": return_case.get("product_name"),
            "sku": return_case.get("sku"),
            "delivery_date": return_case.get("delivery_date"),
            "eligibility": return_case.get("eligibility_result"),
            "resolution": scenario["resolution"],
            "tracking_number": return_case.get("tracking_number"),
            "replacement_order_number": return_case.get("replacement_order_number"),
            "refund_status": return_case.get("refund_status"),
            "replacement_status": return_case.get("replacement_status"),
        },
        "customer_history": history,
        "evidence": evidence,
        "integrations": return_case.get("integration_state") or _integration_state(),
        "events": list(reversed(database.get_timeline(return_case["id"])[-6:])),
    }
