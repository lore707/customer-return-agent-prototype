"""Return Operations MVP: intake, pratiche persistenti e approvazione umana."""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

import agent  # noqa: E402
import conversation  # noqa: E402
import database  # noqa: E402
import demo  # noqa: E402
import domain  # noqa: E402
import policy_config  # noqa: E402
import return_shipping  # noqa: E402
import shopify_client  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
database.init_database()

MAX_MESSAGE_LENGTH = 5_000
DEMO_MODE = os.getenv("DEMO_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}


def _has_live_credentials() -> bool:
    return all(
        (os.getenv(name) or "").strip()
        for name in ("ANTHROPIC_API_KEY", "SHOPIFY_STORE", "SHOPIFY_TOKEN")
    )


def _ensure_demo_showcase() -> None:
    if not DEMO_MODE or app.config.get("TESTING"):
        return
    try:
        demo.ensure_showcase()
    except Exception:  # pragma: no cover - il live intake deve restare disponibile
        logger.exception("Impossibile inizializzare gli scenari portfolio")


def _apply_policy_timeouts() -> None:
    policy = policy_config.load_policy()
    database.apply_policy_timeouts(
        no_response_days=policy["evidence"]["no_response_close_days"],
        unshipped_days=policy["return_logistics"]["unshipped_label_close_days"],
    )


@app.context_processor
def inject_app_shell():
    live_available = _has_live_credentials()
    return {
        "demo_mode": DEMO_MODE,
        "live_intake_available": live_available,
        "integration_status": {
            "shopify": "Live API" if live_available else "Snapshot verificato",
            "claude": "Claude live" if live_available else "Output registrati",
            "shipping": "Provider mock",
        },
        "policy_status": policy_config.summary(),
    }


def _first_product(order: dict | None) -> dict:
    products = (order or {}).get("prodotti") or []
    return products[0] if products else {}


def _sanitized_order_snapshot(order: dict, context: dict) -> dict:
    product = _first_product(order)
    return {
        "store": os.getenv("SHOPIFY_STORE"),
        "order_id": order.get("shopify_order_id"),
        "order_number": order.get("numero_ordine") or context.get("numero_ordine"),
        "customer_id": order.get("customer_id"),
        "customer": order.get("customer_name"),
        "product": product.get("titolo"),
        "sku": product.get("sku"),
        "quantity": product.get("quantita"),
        "financial_status": order.get("stato_finanziario"),
        "fulfillment_status": order.get("stato_evasione"),
        "test_order": True,
    }


def _create_return_case(
    session_id: str, result: dict, analysis_duration_ms: int | None = None
) -> dict:
    package = result["pacchetto"]
    context = package["contesto"]
    action = package["azione_proposta"]
    order = context.get("ordine") or {}
    product = _first_product(order)
    category = context.get("categoria") or "altro"
    confidence = context.get("confidence")
    customer_message = conversation.trascrizione_cliente(session_id)

    return_case = database.create_case(
        {
            "session_id": session_id,
            "shopify_order_id": str(order.get("shopify_order_id") or "") or None,
            "shopify_order_number": context.get("numero_ordine"),
            "customer_id": str(order.get("customer_id") or "") or None,
            "customer_name": order.get("customer_name"),
            "customer_email": order.get("email_cliente"),
            "product_name": product.get("titolo"),
            "sku": product.get("sku"),
            "variant_id": str(product.get("variant_id") or "") or None,
            "line_item_id": str(product.get("line_item_id") or "") or None,
            "quantity": product.get("quantita"),
            "purchase_date": order.get("data_acquisto"),
            "delivery_date": context.get("data_consegna"),
            "request_date": database.utc_now(),
            "return_type": domain.RETURN_TYPE_BY_CATEGORY.get(category, "escalation"),
            "return_reason": category,
            "detailed_reason": customer_message,
            "customer_message": customer_message,
            "ai_classification": {
                "categoria": category,
                "numero_ordine": context.get("numero_ordine"),
                "confidence": confidence,
                "requested_resolution": context.get("requested_resolution"),
                "escalation_reason": context.get("escalation_reason"),
            },
            "confidence": confidence,
            "eligibility_result": domain.eligibility_for_outcome(
                action["esito_proposto"]
            ),
            "policy_applied": context.get("regola_applicata"),
            "policy_decision": package.get("policy_evaluation") or {},
            "suggested_resolution": action["esito_proposto"],
            "original_suggested_response": package.get("bozza_risposta"),
            "analysis_duration_ms": analysis_duration_ms,
            "data_source": "Shopify Admin API",
            "source_mode": "live_api",
            "source_fetched_at": database.utc_now(),
            "source_payload": _sanitized_order_snapshot(order, context),
            "ai_mode": "live_claude",
        }
    )
    database.link_session_messages(
        session_id,
        return_case["id"],
        customer_id=return_case.get("customer_id"),
        customer_email=return_case.get("customer_email"),
    )
    return_case = database.transition_case(
        return_case["id"],
        domain.CaseStatus.ANALYZED.value,
        event_type="ai_analysis_completed",
        details={"classification": return_case["ai_classification"]},
    )

    if action["esito_proposto"] == "escalation_operatore":
        target = domain.CaseStatus.ESCALATED.value
        event = "case_escalated"
    else:
        target = domain.CaseStatus.WAITING_HUMAN_APPROVAL.value
        event = "policy_evaluated"
    return database.transition_case(
        return_case["id"],
        target,
        event_type=event,
        details={
            "outcome": action["esito_proposto"],
            "policy": context.get("regola_applicata"),
        },
    )


def _customer_transcript(case_id: str) -> str:
    return "\n".join(
        message["message"]
        for message in database.get_case_messages(case_id)
        if message["role"] == "cliente"
    )


def _update_existing_case(
    return_case: dict, session_id: str, result: dict, analysis_duration_ms: int
) -> dict:
    """Aggiunge una nuova richiesta alla pratica aperta senza duplicarla."""
    package = result["pacchetto"]
    context = package["contesto"]
    action = package["azione_proposta"]
    database.link_session_messages(
        session_id,
        return_case["id"],
        customer_id=return_case.get("customer_id"),
        customer_email=return_case.get("customer_email"),
    )
    transcript = _customer_transcript(return_case["id"])
    updates = {
        "customer_message": transcript,
        "detailed_reason": transcript,
        "ai_classification": {
            "categoria": context.get("categoria"),
            "numero_ordine": context.get("numero_ordine"),
            "confidence": context.get("confidence"),
            "requested_resolution": context.get("requested_resolution"),
            "escalation_reason": context.get("escalation_reason"),
        },
        "confidence": context.get("confidence"),
        "analysis_duration_ms": analysis_duration_ms,
    }
    analysis_states = {
        domain.CaseStatus.ANALYZED.value,
        domain.CaseStatus.NEEDS_INFORMATION.value,
        domain.CaseStatus.WAITING_HUMAN_APPROVAL.value,
    }
    if return_case["status"] in analysis_states:
        updates.update(
            {
                "return_reason": context.get("categoria") or return_case["return_reason"],
                "return_type": domain.RETURN_TYPE_BY_CATEGORY.get(
                    context.get("categoria"), "escalation"
                ),
                "eligibility_result": domain.eligibility_for_outcome(
                    action["esito_proposto"]
                ),
                "policy_applied": context.get("regola_applicata"),
                "policy_decision": package.get("policy_evaluation") or {},
                "suggested_resolution": action["esito_proposto"],
                "original_suggested_response": package.get("bozza_risposta"),
            }
        )
    updated = database.update_case(
        return_case["id"],
        updates,
        event_type="customer_followup_analyzed",
        event_details={"outcome": action["esito_proposto"]},
    )

    if return_case["status"] not in analysis_states:
        return updated
    if action["esito_proposto"] == "escalation_operatore":
        target = domain.CaseStatus.ESCALATED.value
    elif action["esito_proposto"] in domain.NEEDS_INFORMATION_OUTCOMES:
        target = domain.CaseStatus.NEEDS_INFORMATION.value
    else:
        target = domain.CaseStatus.WAITING_HUMAN_APPROVAL.value
    if updated["status"] != target:
        updated = database.transition_case(
            updated["id"],
            target,
            event_type="followup_status_updated",
            details={"outcome": action["esito_proposto"]},
        )
    return updated


def _case_or_404(case_id: str):
    return_case = database.get_case(case_id)
    if return_case is None:
        return None, (jsonify({"errore": "Pratica non trovata."}), 404)
    return return_case, None


@app.get("/")
def index():
    _ensure_demo_showcase()
    _apply_policy_timeouts()
    filters = {
        "status": (request.args.get("status") or "").strip() or None,
        "reason": (request.args.get("reason") or "").strip() or None,
        "query": (request.args.get("q") or "").strip() or None,
    }
    cases = database.list_cases(**filters)
    scenario_cards = []
    if DEMO_MODE:
        for scenario in demo.SCENARIOS:
            scenario_cards.append(
                {**scenario, "case": database.get_case_by_scenario(scenario["slug"])}
            )
    return render_template(
        "index.html",
        cases=cases,
        metrics=database.analytics(),
        filters=filters,
        statuses=[status.value for status in domain.CaseStatus],
        status_labels=domain.STATUS_LABELS,
        reasons=sorted({case["return_reason"] for case in database.list_cases()}),
        scenarios=scenario_cards,
    )


@app.get("/dashboard")
def dashboard():
    """Panoramica operativa separata dalla landing portfolio."""
    _ensure_demo_showcase()
    _apply_policy_timeouts()
    all_cases = database.list_cases(limit=500)
    waiting_approval = sum(
        item["status"] == domain.CaseStatus.WAITING_HUMAN_APPROVAL.value
        for item in all_cases
    )
    needs_information = sum(
        item["status"] == domain.CaseStatus.NEEDS_INFORMATION.value
        for item in all_cases
    )
    escalated = sum(
        item["status"] == domain.CaseStatus.ESCALATED.value for item in all_cases
    )
    today = datetime.now(timezone.utc).date().isoformat()
    closed_today = sum(
        (item.get("closed_at") or "").startswith(today) for item in all_cases
    )
    decided = [item for item in all_cases if item.get("human_decision")]
    approved_unchanged = sum(
        item.get("human_decision") == "approved" for item in decided
    )
    approval_rate = round(approved_unchanged * 100 / len(decided)) if decided else 0

    priority_order = {
        domain.CaseStatus.WAITING_HUMAN_APPROVAL.value: 0,
        domain.CaseStatus.NEEDS_INFORMATION.value: 1,
        domain.CaseStatus.ESCALATED.value: 2,
        domain.CaseStatus.RETURN_RECEIVED.value: 3,
    }
    priority_cases = sorted(
        [item for item in all_cases if item["status"] != domain.CaseStatus.CLOSED.value],
        key=lambda item: (priority_order.get(item["status"], 9), item["updated_at"]),
    )[:5]

    outcome_definitions = [
        ("Idoneo / risposta pronta", "eligible", "green"),
        ("Informazioni necessarie", "needs_information", "blue"),
        ("Non idoneo", "not_eligible", "amber"),
        ("Escalation", "manual_review", "rose"),
    ]
    outcomes = [
        {
            "label": label,
            "key": key,
            "tone": tone,
            "count": sum(item["eligibility_result"] == key for item in all_cases),
        }
        for label, key, tone in outcome_definitions
    ]
    return render_template(
        "dashboard.html",
        stats={
            "total": len(all_cases),
            "waiting_approval": waiting_approval,
            "needs_information": needs_information,
            "escalated": escalated,
            "closed_today": closed_today,
            "approval_rate": approval_rate,
        },
        priority_cases=priority_cases,
        outcomes=outcomes,
        reasons=database.analytics()["by_reason"],
        activities=database.recent_activity(limit=5),
        status_labels=domain.STATUS_LABELS,
    )


@app.get("/database")
def database_register():
    _ensure_demo_showcase()
    _apply_policy_timeouts()
    filters = {
        "status": (request.args.get("status") or "").strip() or None,
        "reason": (request.args.get("reason") or "").strip() or None,
        "query": (request.args.get("q") or "").strip() or None,
    }
    cases = database.list_cases(**filters, limit=500)
    counts = database.message_counts([item["id"] for item in cases])
    for item in cases:
        item["message_count"] = counts.get(item["id"], 0)
    return render_template(
        "database.html",
        cases=cases,
        metrics=database.analytics(),
        filters=filters,
        statuses=[status.value for status in domain.CaseStatus],
        status_labels=domain.STATUS_LABELS,
        reasons=sorted({case["return_reason"] for case in database.list_cases()}),
        message_total=sum(counts.values()),
    )


def _policy_rule_count(value) -> int:
    if isinstance(value, dict):
        return sum(_policy_rule_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_policy_rule_count(item) for item in value)
    return 1


@app.get("/policies")
def policies():
    policy = policy_config.load_policy()
    policy_cards = [
        {
            "id": "standard",
            "title": "Policy Resi Standard",
            "description": "Recesso, rimborso e logistica di rientro",
            "status": "Attiva",
        },
        {
            "id": "hygiene",
            "title": "Policy Prodotti Igienici",
            "description": "Sigilli, esclusioni e condizioni prodotto",
            "status": "Attiva",
        },
        {
            "id": "exceptions",
            "title": "Policy Eccezioni e Garanzia",
            "description": "DOA, danni, articolo errato ed escalation",
            "status": "Attiva",
        },
    ]
    selected_id = request.args.get("policy", "standard")
    if selected_id not in {item["id"] for item in policy_cards}:
        selected_id = "standard"
    selected = next(item for item in policy_cards if item["id"] == selected_id)
    standard = policy["withdrawal"]
    if selected_id == "standard":
        rules_view = [
            ("Finestra di reso", f"{standard['window_days']} giorni dalla consegna"),
            ("Condizioni prodotto", "Integro e rivendibile"),
            ("Costi spedizione reso", "A carico del cliente"),
            ("Azioni disponibili", "Rimborso"),
            ("Imballo esterno", "Obbligatorio"),
            ("Controllo fisico", "Prima del rimborso"),
        ]
        description = "Regole applicate alle richieste di recesso standard entro i termini."
    elif selected_id == "hygiene":
        rules_view = [
            ("Prodotti esclusi", ", ".join(standard["excluded_product_keywords"])),
            ("Prodotto sigillato", "Idoneo alla valutazione"),
            ("Prodotto aperto", "Recesso non idoneo"),
            ("Difetto dichiarato", "Passa al flusso garanzia"),
            ("Stato sigillo assente", "Chiedi informazione al cliente"),
            ("Controllo fisico", "Obbligatorio al rientro"),
        ]
        description = "Eccezioni per prodotti personali: la decisione dipende dallo stato del sigillo."
    else:
        defective = policy["defective_product"]
        rules_view = [
            ("Prove richieste", ", ".join(defective["evidence_required"])),
            ("Difetto nei 14 giorni", "Rimborso o sostituzione"),
            ("Difetto oltre 14 giorni", "Sostituzione"),
            ("Danno da trasporto", "Rimborso, spedizione azienda"),
            ("Articolo errato", "Revisione umana"),
            ("Bassa confidenza", f"Escalation sotto {policy['escalation']['low_confidence_below']:.0%}"),
        ]
        description = "Regole per difetti, danni e casi che non possono essere decisi automaticamente."
    return render_template(
        "policies.html",
        policy=policy,
        policy_cards=policy_cards,
        selected=selected,
        selected_id=selected_id,
        rules_view=rules_view,
        description=description,
        rule_count=_policy_rule_count(policy) - _policy_rule_count(policy["unresolved"]),
    )


def _chart_points(rows: list[dict], key: str, *, width: int = 100, height: int = 42) -> str:
    values = [int(item.get(key) or 0) for item in rows]
    maximum = max(values or [1]) or 1
    last = max(len(values) - 1, 1)
    return " ".join(
        f"{index * width / last:.1f},{height - (value * (height - 7) / maximum):.1f}"
        for index, value in enumerate(values)
    )


@app.get("/analytics")
def analytics_view():
    _ensure_demo_showcase()
    metrics = database.performance_analytics()
    palette = ["#835bff", "#496cff", "#e45a78", "#43b8b0", "#69748e"]
    total_reasons = sum(item["count"] for item in metrics["reasons"]) or 1
    decorated_reasons = []
    current = 0.0
    gradient_stops = []
    for index, item in enumerate(metrics["reasons"]):
        percentage = item["count"] * 100 / total_reasons
        color = palette[index % len(palette)]
        decorated_reasons.append(
            {**item, "percentage": round(percentage), "color": color}
        )
        gradient_stops.append(
            f"{color} {current:.1f}% {current + percentage:.1f}%"
        )
        current += percentage
    feedback_labels = {
        "policy_interpretation": "Interpretazione policy",
        "missing_information": "Informazioni mancanti",
        "tone_style": "Tono / stile inappropriato",
        "too_verbose": "Troppo prolisso",
        "incorrect_action": "Azione non corretta",
        "other": "Altro",
    }
    feedback = [
        {**item, "label": feedback_labels.get(item["reason_tag"], item["reason_tag"])}
        for item in metrics["feedback"]
    ]
    return render_template(
        "analytics.html",
        metrics=metrics,
        reasons=decorated_reasons,
        feedback=feedback,
        donut_gradient=", ".join(gradient_stops) or "#283149 0 100%",
        chart={
            "total": _chart_points(metrics["daily"], "total"),
            "approved": _chart_points(metrics["daily"], "approved"),
            "escalated": _chart_points(metrics["daily"], "escalated"),
        },
    )


@app.get("/cases/<case_id>")
def case_detail(case_id: str):
    _apply_policy_timeouts()
    return_case = database.get_case(case_id)
    if return_case is None:
        return render_template("404.html"), 404
    timeline = database.get_timeline(case_id)
    event_types = {event["event_type"] for event in timeline}
    if return_case["suggested_resolution"] in (
        {*domain.REJECTION_OUTCOMES, "escalation_operatore"}
    ) or return_case["status"] == domain.CaseStatus.ESCALATED.value:
        milestone_defs = [
            ("Richiesta", {"customer_request_received"}),
            ("Analisi", {"ai_analysis_completed"}),
            ("Revisione", {"human_approval_recorded", "case_escalated"}),
            ("Chiusura", {"case_closed"}),
        ]
    else:
        milestone_defs = [
            ("Richiesta", {"customer_request_received"}),
            ("Analisi", {"ai_analysis_completed"}),
            ("Approvazione", {"human_approval_recorded"}),
            ("Rientro", {"return_received"}),
            ("Controllo", {"physical_inspection_passed"}),
            ("Risoluzione", {"case_closed"}),
        ]
    milestones = []
    active_assigned = False
    for label, event_names in milestone_defs:
        completed = bool(event_names & event_types)
        active = not completed and not active_assigned
        active_assigned = active_assigned or active
        milestones.append({"label": label, "completed": completed, "active": active})
    scenario = demo.get_scenario(return_case.get("scenario_slug"))
    return render_template(
        "case_detail.html",
        case=return_case,
        timeline=timeline,
        messages=database.get_case_messages(case_id),
        status_labels=domain.STATUS_LABELS,
        source_payload=json.dumps(
            return_case.get("source_payload") or {}, ensure_ascii=False, indent=2
        ),
        scenario=scenario,
        milestones=milestones,
    )


def _render_workbench(case_id: str):
    _apply_policy_timeouts()
    return_case = database.get_case(case_id)
    if return_case is None:
        return render_template("404.html"), 404
    messages = database.get_case_messages(case_id)
    timeline = database.get_timeline(case_id)
    cases = database.list_cases(limit=100)
    review_statuses = {domain.CaseStatus.WAITING_HUMAN_APPROVAL.value}
    escalated_statuses = {domain.CaseStatus.ESCALATED.value}
    case_counts = {
        "all": len(cases),
        "review": sum(item["status"] in review_statuses for item in cases),
        "escalated": sum(item["status"] in escalated_statuses for item in cases),
        "closed": sum(item["status"] == domain.CaseStatus.CLOSED.value for item in cases),
    }
    previous_return_count = sum(
        item["id"] != return_case["id"]
        and item.get("shopify_order_number")
        == return_case.get("shopify_order_number")
        for item in cases
    )
    return render_template(
        "workbench.html",
        case=return_case,
        messages=messages,
        timeline=timeline,
        feedback=database.get_case_feedback(case_id),
        status_labels=domain.STATUS_LABELS,
        cases=cases,
        case_counts=case_counts,
        previous_return_count=previous_return_count,
        source_payload=json.dumps(
            return_case.get("source_payload") or {}, ensure_ascii=False, indent=2
        ),
        scenario=demo.get_scenario(return_case.get("scenario_slug")),
    )


@app.get("/workbench")
def workbench():
    _ensure_demo_showcase()
    requested = (request.args.get("case_id") or "").strip()
    if requested and database.get_case(requested):
        return redirect(url_for("workbench_case", case_id=requested))
    cases = database.list_cases(limit=100)
    priority = (
        domain.CaseStatus.WAITING_HUMAN_APPROVAL.value,
        domain.CaseStatus.NEEDS_INFORMATION.value,
        domain.CaseStatus.RETURN_RECEIVED.value,
    )
    selected = next(
        (item for status in priority for item in cases if item["status"] == status),
        cases[0] if cases else None,
    )
    if selected is None:
        return redirect(url_for("index"))
    return redirect(url_for("workbench_case", case_id=selected["id"]))


@app.get("/workbench/<case_id>")
def workbench_case(case_id: str):
    return _render_workbench(case_id)


@app.post("/messaggio")
def messaggio():
    data = request.get_json(silent=True) or {}
    requested_case_id = (data.get("case_id") or "").strip()
    session_id = (data.get("session_id") or "").strip()
    text = (data.get("messaggio") or "").strip()
    requested_case = None
    if requested_case_id:
        requested_case = database.get_case(requested_case_id)
        if requested_case is None:
            return jsonify({"errore": "Pratica non trovata."}), 404
        session_id = requested_case.get("session_id") or session_id
    if not session_id or len(session_id) > 100:
        return jsonify({"errore": "session_id mancante o non valido."}), 400
    if not text:
        return jsonify({"errore": "Messaggio vuoto."}), 400
    if len(text) > MAX_MESSAGE_LENGTH:
        return jsonify({"errore": "Messaggio troppo lungo."}), 400

    try:
        stored_messages = database.get_session_messages(session_id)
        if stored_messages:
            conversation.ripristina_sessione(session_id, stored_messages)
        database.add_message(
            session_id,
            "cliente",
            text,
            case_id=requested_case_id or None,
            customer_id=(requested_case or {}).get("customer_id"),
            customer_email=(requested_case or {}).get("customer_email"),
            message_type="customer_followup" if requested_case_id else "customer_request",
        )
        started = time.perf_counter()
        result = conversation.processa_messaggio(session_id, text)
        if result.get("tipo") == "richiesta_info":
            database.add_message(
                session_id,
                "agente",
                result["risposta_agente"],
                case_id=requested_case_id or None,
                customer_id=(requested_case or {}).get("customer_id"),
                customer_email=(requested_case or {}).get("customer_email"),
                message_type="agent_question",
            )
            if requested_case:
                transcript = _customer_transcript(requested_case["id"])
                database.update_case(
                    requested_case["id"],
                    {"customer_message": transcript, "detailed_reason": transcript},
                    event_type="customer_followup_received",
                )
                result["case_id"] = requested_case["id"]
                result["case_status"] = requested_case["status"]
        elif result.get("tipo") == "pacchetto":
            duration_ms = round((time.perf_counter() - started) * 1000)
            context = result["pacchetto"]["contesto"]
            order = context.get("ordine") or {}
            return_case = requested_case or database.find_open_case(
                context.get("numero_ordine"), str(order.get("customer_id") or "") or None
            )
            if return_case:
                return_case = _update_existing_case(
                    return_case, session_id, result, duration_ms
                )
            else:
                return_case = _create_return_case(session_id, result, duration_ms)
            result["case_id"] = return_case["id"]
            result["case_status"] = return_case["status"]
        return jsonify(result)
    except Exception:  # noqa: BLE001 - la route non espone dettagli interni
        logger.exception("Errore durante l'analisi della richiesta")
        return jsonify({"errore": "Impossibile analizzare la richiesta."}), 500


@app.post("/conferma")
def conferma():
    data = request.get_json(silent=True) or {}
    action = data.get("azione")
    case_id = (data.get("case_id") or "").strip()
    if action not in {"accetta", "rifiuta", "escala"} or not case_id:
        return jsonify({"errore": "Azione o pratica non valida."}), 400

    return_case, error = _case_or_404(case_id)
    if error:
        return error
    if return_case["status"] != domain.CaseStatus.WAITING_HUMAN_APPROVAL.value:
        return jsonify({"errore": "La pratica non è in attesa di approvazione."}), 409

    final_response = (data.get("testo_finale") or "").strip() or None
    human_reason = (data.get("motivazione") or "").strip() or None
    feedback_tag = (data.get("feedback_tag") or "").strip() or None
    original = return_case.get("original_suggested_response") or ""
    created_at = datetime.fromisoformat(return_case["created_at"])
    operator_seconds = max(
        0, round((datetime.now(timezone.utc) - created_at).total_seconds())
    )

    if action in {"rifiuta", "escala"}:
        decision = "rejected_suggestion" if action == "rifiuta" else "escalated"
        database.add_operator_feedback(
            case_id,
            decision,
            reason_tag=feedback_tag,
            instructions=human_reason,
            original_draft=original,
        )
        database.update_case(
            case_id,
            {
                "human_decision": decision,
                "human_reason": human_reason,
                "operator_decision_seconds": operator_seconds,
                "manual_step_count": (return_case.get("manual_step_count") or 0) + 1,
            },
            event_type="human_decision_recorded",
            event_details={"decision": decision, "reason": human_reason},
        )
        updated = database.transition_case(
            case_id,
            domain.CaseStatus.ESCALATED.value,
            event_type="case_escalated",
            details={"by": "operator", "reason": human_reason},
        )
        conversation.chiudi_sessione(return_case.get("session_id") or "")
        return jsonify({"ok": True, "case": updated})

    if not final_response:
        final_response = original
    database.add_message(
        return_case.get("session_id") or f"case-{case_id}",
        "operatore",
        final_response,
        case_id=case_id,
        customer_id=return_case.get("customer_id"),
        customer_email=return_case.get("customer_email"),
        message_type="approved_response",
        metadata={
            "sent": True,
            "delivery_mode": "portfolio_simulation" if DEMO_MODE else "operator_channel",
        },
    )
    modified = final_response != original
    if modified or feedback_tag or human_reason:
        database.add_operator_feedback(
            case_id,
            "modified_and_approved" if modified else "approved_with_feedback",
            reason_tag=feedback_tag,
            instructions=human_reason,
            original_draft=original,
            revised_draft=final_response,
        )
    database.update_case(
        case_id,
        {
            "human_decision": "modified_and_approved" if modified else "approved",
            "human_reason": human_reason,
            "final_response": final_response,
            "operator_decision_seconds": operator_seconds,
            "manual_step_count": (return_case.get("manual_step_count") or 0) + 1,
        },
        event_type="human_approval_recorded",
        event_details={"modified": modified, "reason": human_reason},
    )

    outcome = return_case["suggested_resolution"]
    if outcome in domain.NEEDS_INFORMATION_OUTCOMES:
        updated = database.transition_case(
            case_id,
            domain.CaseStatus.NEEDS_INFORMATION.value,
            event_type="additional_information_requested",
            details={"outcome": outcome},
        )
        conversation.riapri_raccolta(return_case.get("session_id") or "")
        return jsonify({"ok": True, "case": updated})

    if outcome in domain.REJECTION_OUTCOMES:
        updated = database.transition_case(
            case_id,
            domain.CaseStatus.REJECTED.value,
            event_type="return_rejected",
            details={"outcome": outcome},
        )
        conversation.chiudi_sessione(
            return_case.get("session_id") or "", final_response
        )
        return jsonify({"ok": True, "case": updated})

    if outcome not in domain.LABEL_OUTCOMES:
        updated = database.transition_case(
            case_id,
            domain.CaseStatus.ESCALATED.value,
            event_type="case_escalated",
            details={"reason": "Esito non automatizzabile"},
        )
        return jsonify({"ok": True, "case": updated})

    database.transition_case(
        case_id,
        domain.CaseStatus.APPROVED.value,
        event_type="return_approved",
        details={"outcome": outcome},
    )
    shipment = return_shipping.get_provider().create_return(database.get_case(case_id))
    database.update_case(
        case_id,
        {
            "label_status": "created",
            "shipping_provider": shipment["provider"],
            "sendcloud_return_id": shipment["return_id"],
            "tracking_number": shipment["tracking_number"],
            "label_url": shipment["label_url"],
            "api_action_count": (return_case.get("api_action_count") or 0) + 1,
        },
        event_type="return_label_generated",
        event_details=shipment,
    )
    database.transition_case(
        case_id,
        domain.CaseStatus.LABEL_CREATED.value,
        event_type="label_created",
        details={"provider": shipment["provider"]},
    )
    updated = database.transition_case(
        case_id,
        domain.CaseStatus.WAITING_FOR_RETURN.value,
        event_type="waiting_for_customer_return",
    )
    conversation.chiudi_sessione(return_case.get("session_id") or "", final_response)
    return jsonify({"ok": True, "case": updated})


def _recorded_regenerated_draft(return_case: dict, instructions: str = "") -> str:
    """Bozza deterministica per la demo pubblica: nessuna chiamata AI a pagamento."""
    name = (return_case.get("customer_name") or "").split(" ")[0] or "cliente"
    order = return_case.get("shopify_order_number") or "—"
    outcome = return_case.get("suggested_resolution")
    decision = return_case.get("policy_decision") or {}
    formal = "formal" in instructions.lower()
    greeting = f"Gentile {name}," if formal else f"Ciao {name},"
    shipping = decision.get("shipping_payer")
    shipping_text = (
        "La spedizione di reso è a carico dell'azienda."
        if shipping == "company"
        else "Il costo della spedizione di reso sarà comunicato prima della conferma e detratto dal rimborso."
    )
    templates = {
        "procedi_rimborso": (
            f"{greeting}\n\nla richiesta relativa all'ordine #{order} rispetta i requisiti della policy. "
            f"{shipping_text} Il rimborso verrà autorizzato solo dopo il rientro e il controllo fisico del prodotto.\n\n"
            "A presto,\nCustomer Care Team"
        ),
        "procedi_swap": (
            f"{greeting}\n\nle verifiche sull'ordine #{order} consentono di proporre una sostituzione. "
            f"{shipping_text} La sostituzione partirà dopo il rientro e il controllo fisico.\n\n"
            "A presto,\nCustomer Care Team"
        ),
        "chiedi_foto_video": (
            f"{greeting}\n\nper verificare il problema dell'ordine #{order}, inviaci un breve video del difetto "
            "e una foto dell'etichetta seriale. La pratica sarà rivalutata appena riceveremo le prove.\n\n"
            "Grazie,\nCustomer Care Team"
        ),
        "chiedi_stato_sigillo": (
            f"{greeting}\n\nprima di valutare il recesso dell'ordine #{order}, confermaci se confezione e sigillo "
            "sono integri e se il prodotto è stato aperto o utilizzato.\n\nGrazie,\nCustomer Care Team"
        ),
        "offri_scelta_rimborso_o_swap": (
            f"{greeting}\n\nper l'ordine #{order} puoi scegliere tra rimborso e sostituzione. "
            "Indicaci l'opzione che preferisci e prepareremo il passaggio successivo.\n\nCustomer Care Team"
        ),
        "rifiuta_recesso_prodotto_escluso": (
            f"{greeting}\n\nnon possiamo approvare il recesso dell'ordine #{order}: il prodotto per uso personale "
            "risulta aperto. Un eventuale difetto resta comunque coperto dalla garanzia.\n\nCustomer Care Team"
        ),
        "rifiuta_fuori_finestra": (
            f"{greeting}\n\nla richiesta per l'ordine #{order} è oltre i 14 giorni previsti dalla consegna. "
            "Per questo il recesso non può essere approvato.\n\nCustomer Care Team"
        ),
    }
    return templates.get(outcome, return_case.get("original_suggested_response") or "")


@app.post("/cases/<case_id>/draft-action")
def draft_action(case_id: str):
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip()
    reason_tag = (data.get("reason_tag") or "").strip() or None
    instructions = (data.get("instructions") or "").strip()
    return_case, error = _case_or_404(case_id)
    if error:
        return error
    if return_case["status"] != domain.CaseStatus.WAITING_HUMAN_APPROVAL.value:
        return jsonify({"errore": "La bozza non è più in attesa di revisione."}), 409
    if action not in {"regenerate", "discard"}:
        return jsonify({"errore": "Azione bozza non valida."}), 400

    original = return_case.get("original_suggested_response") or ""
    if action == "discard":
        database.add_operator_feedback(
            case_id,
            "discarded",
            reason_tag=reason_tag,
            instructions=instructions,
            original_draft=original,
        )
        database.update_case(
            case_id,
            {"human_decision": "rejected_suggestion", "human_reason": instructions},
            event_type="draft_discarded",
            event_details={"reason_tag": reason_tag},
        )
        updated = database.transition_case(
            case_id,
            domain.CaseStatus.ESCALATED.value,
            event_type="case_escalated",
            details={"reason": instructions or "Bozza scartata dall'operatore"},
        )
        return jsonify({"ok": True, "case": updated})

    if not instructions and not reason_tag:
        return jsonify({"errore": "Indica almeno il motivo della rigenerazione."}), 400
    try:
        if return_case.get("source_mode") == "recorded_fixture":
            revised = _recorded_regenerated_draft(return_case, instructions)
            generation_mode = "recorded_demo"
        else:
            revised = agent.rigenera_bozza(
                return_case,
                instructions or reason_tag or "Rendi la risposta più chiara.",
                feedback_examples=database.feedback_examples(
                    return_case["return_reason"]
                ),
            )
            generation_mode = "live_claude"
    except Exception:  # noqa: BLE001
        logger.exception("Rigenerazione bozza non riuscita")
        return jsonify({"errore": "Non è stato possibile rigenerare la bozza."}), 502
    if not revised:
        return jsonify({"errore": "La rigenerazione non ha prodotto testo."}), 502
    database.add_operator_feedback(
        case_id,
        "regenerated",
        reason_tag=reason_tag,
        instructions=instructions,
        original_draft=original,
        revised_draft=revised,
    )
    updated = database.update_case(
        case_id,
        {"original_suggested_response": revised, "final_response": None},
        event_type="draft_regenerated",
        event_details={"mode": generation_mode, "reason_tag": reason_tag},
    )
    return jsonify({"ok": True, "case": updated, "draft": revised})


@app.post("/cases/<case_id>/simulate-message")
def simulate_customer_message(case_id: str):
    """Continua una conversazione nello showcase senza Shopify/Claude live."""
    data = request.get_json(silent=True) or {}
    text = (data.get("message") or "").strip()
    return_case, error = _case_or_404(case_id)
    if error:
        return error
    if return_case.get("source_mode") != "recorded_fixture":
        return jsonify({"errore": "Per una pratica live usa il canale di intake."}), 409
    if return_case["status"] == domain.CaseStatus.CLOSED.value:
        return jsonify({"errore": "La pratica è chiusa."}), 409
    if not text or len(text) > MAX_MESSAGE_LENGTH:
        return jsonify({"errore": "Scrivi un messaggio valido."}), 400

    database.add_message(
        return_case.get("session_id") or f"case-{case_id}",
        "cliente",
        text,
        case_id=case_id,
        customer_id=return_case.get("customer_id"),
        customer_email=return_case.get("customer_email"),
        message_type="customer_followup",
        metadata={"simulated": True},
    )
    database.update_case(
        case_id,
        {"customer_message": _customer_transcript(case_id), "detailed_reason": _customer_transcript(case_id)},
        event_type="customer_followup_received",
        event_details={"mode": "customer_simulator"},
    )
    lowered = text.lower()
    current = database.get_case(case_id)
    if current["status"] == domain.CaseStatus.NEEDS_INFORMATION.value:
        outcome = current.get("suggested_resolution")
        if outcome == "chiedi_foto_video" and any(
            word in lowered for word in ("foto", "video", "allego", "prova", "seriale")
        ):
            category = (
                "arrivato_rotto"
                if any(word in lowered for word in ("corriere", "pacco", "trasporto"))
                else current["return_reason"]
            )
            current = _reevaluate_case(
                current, prove_fornite=True, category_override=category
            )
        elif outcome == "chiedi_stato_sigillo" and any(
            word in lowered for word in ("aperto", "usato", "integro", "sigill")
        ):
            intact = not any(word in lowered for word in ("aperto", "usato", "rotto"))
            current = _reevaluate_case(current, sigillo_integro=intact)
        elif outcome == "offri_scelta_rimborso_o_swap" and any(
            word in lowered for word in ("rimborso", "sostituzione", "swap")
        ):
            choice = "refund" if "rimborso" in lowered else "swap"
            current = _reevaluate_case(current, requested_resolution=choice)
    elif current["status"] == domain.CaseStatus.WAITING_HUMAN_APPROVAL.value:
        revised = _recorded_regenerated_draft(current, text)
        current = database.update_case(
            case_id,
            {"original_suggested_response": revised},
            event_type="draft_refreshed_after_followup",
            event_details={"mode": "recorded_demo"},
        )
    return jsonify({"ok": True, "case": current})


def _reevaluate_case(
    return_case: dict, *, prove_fornite: bool = False, sigillo_integro=None,
    category_override: str | None = None, requested_resolution: str | None = None
) -> dict:
    requested_resolution = requested_resolution or (
        (return_case.get("ai_classification") or {}).get("requested_resolution")
    )
    if return_case.get("source_mode") == "recorded_fixture":
        return demo.reevaluate_recorded_case(
            return_case,
            evidence_received=prove_fornite,
            seal_intact=sigillo_integro,
            category_override=category_override,
            requested_resolution=requested_resolution,
        )
    order = shopify_client.get_order(return_case["shopify_order_number"])
    if order is None:
        raise ValueError("Ordine non trovato durante la rivalutazione.")
    package = agent.costruisci_pacchetto(
        category_override or return_case["return_reason"],
        return_case["shopify_order_number"],
        dati_ordine=order,
        prove_fornite=prove_fornite or requested_resolution is not None,
        sigillo_integro=sigillo_integro,
        confidence=return_case.get("confidence"),
        requested_resolution=requested_resolution,
    )
    context = package["contesto"]
    action = package["azione_proposta"]
    database.update_case(
        return_case["id"],
        {
            "eligibility_result": domain.eligibility_for_outcome(
                action["esito_proposto"]
            ),
            "return_reason": category_override or return_case["return_reason"],
            "return_type": domain.RETURN_TYPE_BY_CATEGORY.get(
                category_override or return_case["return_reason"], "escalation"
            ),
            "policy_applied": context.get("regola_applicata"),
            "policy_decision": package.get("policy_evaluation") or {},
            "suggested_resolution": action["esito_proposto"],
            "original_suggested_response": package.get("bozza_risposta"),
            "final_response": None,
            "human_decision": None,
            "human_reason": None,
        },
        event_type="case_reevaluated",
        event_details={"outcome": action["esito_proposto"]},
    )
    return database.transition_case(
        return_case["id"],
        domain.CaseStatus.WAITING_HUMAN_APPROVAL.value,
        event_type="waiting_human_approval",
        details={"outcome": action["esito_proposto"]},
    )


@app.post("/cases/<case_id>/evidence")
def evidence_received(case_id: str):
    return_case, error = _case_or_404(case_id)
    if error:
        return error
    if (
        return_case["status"] != domain.CaseStatus.NEEDS_INFORMATION.value
        or return_case["suggested_resolution"] != "chiedi_foto_video"
    ):
        return jsonify({"errore": "La pratica non è in attesa di prove."}), 409
    try:
        data = request.get_json(silent=True) or {}
        evidence_category = data.get("evidence_category") or return_case["return_reason"]
        if evidence_category not in {"doa", "arrivato_rotto"}:
            return jsonify({"errore": "Specificare difetto prodotto o danno da trasporto."}), 400
        conversation.segna_prove_fornite(return_case.get("session_id") or "")
        updated = _reevaluate_case(
            return_case,
            prove_fornite=True,
            category_override=evidence_category,
        )
        return jsonify({"ok": True, "case": updated})
    except (shopify_client.ShopifyError, ValueError, RuntimeError):
        logger.exception("Rivalutazione prove fallita per %s", case_id)
        return jsonify({"errore": "Impossibile rivalutare la pratica."}), 502


@app.post("/cases/<case_id>/resolution-choice")
def resolution_choice(case_id: str):
    """Registra la scelta del cliente nel DOA entro 14 giorni."""
    return_case, error = _case_or_404(case_id)
    if error:
        return error
    choice = (request.get_json(silent=True) or {}).get("choice")
    if (
        return_case["status"] != domain.CaseStatus.NEEDS_INFORMATION.value
        or return_case["suggested_resolution"] != "offri_scelta_rimborso_o_swap"
        or choice not in {"refund", "swap"}
    ):
        return jsonify({"errore": "Scelta non valida per questa pratica."}), 409
    try:
        updated = _reevaluate_case(return_case, requested_resolution=choice)
        database.add_audit_event(
            case_id,
            "customer_resolution_choice_recorded",
            details={"choice": choice, "policy_section": "§1"},
        )
        return jsonify({"ok": True, "case": updated})
    except (shopify_client.ShopifyError, ValueError, RuntimeError):
        logger.exception("Scelta risoluzione fallita per %s", case_id)
        return jsonify({"errore": "Impossibile rivalutare la pratica."}), 502


@app.post("/cases/<case_id>/seal")
def seal_status(case_id: str):
    return_case, error = _case_or_404(case_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    intact = data.get("intact")
    if (
        return_case["status"] != domain.CaseStatus.NEEDS_INFORMATION.value
        or return_case["suggested_resolution"] != "chiedi_stato_sigillo"
        or not isinstance(intact, bool)
    ):
        return jsonify({"errore": "Stato sigillo non valido per questa pratica."}), 409
    try:
        updated = _reevaluate_case(return_case, sigillo_integro=intact)
        return jsonify({"ok": True, "case": updated})
    except (shopify_client.ShopifyError, ValueError, RuntimeError):
        logger.exception("Rivalutazione sigillo fallita per %s", case_id)
        return jsonify({"errore": "Impossibile rivalutare la pratica."}), 502


@app.post("/cases/<case_id>/advance")
def advance_case(case_id: str):
    return_case, error = _case_or_404(case_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    action = data.get("azione")
    note = (data.get("note") or "").strip()
    status = return_case["status"]
    try:
        if action == "in_transit" and status == domain.CaseStatus.WAITING_FOR_RETURN.value:
            updated = database.transition_case(
                case_id,
                domain.CaseStatus.RETURN_IN_TRANSIT.value,
                event_type="return_in_transit",
            )
        elif action == "received" and status in {
            domain.CaseStatus.WAITING_FOR_RETURN.value,
            domain.CaseStatus.RETURN_IN_TRANSIT.value,
        }:
            updated = database.transition_case(
                case_id,
                domain.CaseStatus.RETURN_RECEIVED.value,
                event_type="return_received",
            )
        elif action == "validate_return" and status == domain.CaseStatus.RETURN_RECEIVED.value:
            checks = data.get("checks") or {}
            required_checks = policy_config.load_policy()["physical_inspection"]["checks"]
            if not all(checks.get(item) is True for item in required_checks):
                return jsonify({
                    "errore": "Completa i tre controlli fisici prima di validare il reso."
                }), 409
            updated = database.transition_case(
                case_id,
                domain.CaseStatus.RETURN_VALIDATED.value,
                event_type="physical_inspection_passed",
                details={"note": note, "by": "operator", "checks": checks},
            )
        elif action == "reject_after_inspection" and status == domain.CaseStatus.RETURN_RECEIVED.value:
            if note:
                database.update_case(case_id, {"human_reason": note})
            updated = database.transition_case(
                case_id,
                domain.CaseStatus.ESCALATED.value,
                event_type="physical_inspection_exception_escalated",
                details={
                    "note": note,
                    "by": "operator",
                    "unresolved_policy": "devalued_or_damaged_return_outcome",
                    "policy_sections": ["§4", "§7"],
                },
            )
        elif action == "start_resolution" and status == domain.CaseStatus.RETURN_VALIDATED.value:
            if return_case["suggested_resolution"] == "procedi_swap":
                database.update_case(case_id, {"replacement_status": "pending"})
                target = domain.CaseStatus.REPLACEMENT_PENDING.value
                event = "replacement_started"
            else:
                database.update_case(case_id, {"refund_status": "pending"})
                target = domain.CaseStatus.REFUND_PENDING.value
                event = "refund_started"
            details = (
                {
                    "replacement_order": "single_item_only",
                    "stocked_location_required": True,
                    "policy_sections": ["§6", "§7"],
                }
                if target == domain.CaseStatus.REPLACEMENT_PENDING.value
                else {"policy_section": "§6"}
            )
            updated = database.transition_case(
                case_id, target, event_type=event, details=details
            )
        elif (
            action == "start_refund_no_stock"
            and status == domain.CaseStatus.RETURN_VALIDATED.value
            and return_case["suggested_resolution"] == "procedi_swap"
        ):
            database.update_case(
                case_id,
                {"refund_status": "pending", "replacement_status": "unavailable"},
                event_type="replacement_stock_unavailable",
                event_details={"fallback": "refund", "policy_section": "§1"},
            )
            updated = database.transition_case(
                case_id,
                domain.CaseStatus.REFUND_PENDING.value,
                event_type="refund_started_no_replacement_stock",
            )
        elif action == "close_no_evidence" and status == domain.CaseStatus.NEEDS_INFORMATION.value:
            updated = database.transition_case(
                case_id,
                domain.CaseStatus.CLOSED.value,
                event_type="evidence_not_received",
                details={"policy_section": "§5"},
            )
        elif action == "complete" and status in {
            domain.CaseStatus.REFUND_PENDING.value,
            domain.CaseStatus.REPLACEMENT_PENDING.value,
        }:
            if status == domain.CaseStatus.REFUND_PENDING.value:
                database.update_case(case_id, {"refund_status": "completed"})
                target = domain.CaseStatus.REFUNDED.value
                event = "refund_completed"
            else:
                database.update_case(case_id, {"replacement_status": "completed"})
                target = domain.CaseStatus.REPLACED.value
                event = "replacement_completed"
            database.transition_case(case_id, target, event_type=event)
            updated = database.transition_case(
                case_id, domain.CaseStatus.CLOSED.value, event_type="case_closed"
            )
        elif action == "close" and status in {
            domain.CaseStatus.NEEDS_INFORMATION.value,
            domain.CaseStatus.REJECTED.value,
            domain.CaseStatus.ESCALATED.value,
            domain.CaseStatus.WAITING_FOR_RETURN.value,
            domain.CaseStatus.RETURN_RECEIVED.value,
            domain.CaseStatus.RETURN_VALIDATED.value,
        }:
            updated = database.transition_case(
                case_id, domain.CaseStatus.CLOSED.value, event_type="case_closed"
            )
        else:
            return jsonify({"errore": "Azione non consentita nello stato corrente."}), 409
    except domain.InvalidTransition as exc:
        return jsonify({"errore": str(exc)}), 409
    updated = database.update_case(
        case_id,
        {"manual_step_count": (return_case.get("manual_step_count") or 0) + 1},
    )
    return jsonify({"ok": True, "case": updated})


@app.post("/reset")
def reset():
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    if session_id:
        conversation.reset_sessione(session_id)
    return jsonify({"ok": True})


@app.post("/demo/reset")
def reset_demo():
    if not DEMO_MODE:
        return jsonify({"errore": "Modalità demo non attiva."}), 404
    data = request.get_json(silent=True) or {}
    if data.get("confirm") != "RESET_DEMO":
        return jsonify({"errore": "Conferma reset non valida."}), 400
    cases = demo.reset_showcase()
    return jsonify({"ok": True, "cases": len(cases)})


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "mode": "portfolio_demo" if DEMO_MODE else "live",
            "live_integrations": _has_live_credentials(),
            "database": "sqlite",
            "shipping": "mock",
            "automated_tests": 30,
            "policy_version": policy_config.load_policy()["version"],
        }
    )


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/cases/"):
        return render_template("404.html"), 404
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
