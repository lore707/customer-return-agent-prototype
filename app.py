"""Return Operations MVP: intake, pratiche persistenti e approvazione umana."""

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
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
import guided_demo  # noqa: E402
import policy_config  # noqa: E402
import policy_import  # noqa: E402
import return_shipping  # noqa: E402
import rules  # noqa: E402
import shopify_client  # noqa: E402
import support_copilot  # noqa: E402

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
    return render_template("index.html")


@app.get("/demo/<scenario_slug>")
def guided_demo_view(scenario_slug: str):
    """Presenta uno dei due workflow guidati principali."""
    scenario = guided_demo.get_scenario(scenario_slug)
    if scenario is None:
        return render_template("404.html"), 404
    return render_template("guided_demo.html", scenario=scenario)


@app.post("/api/guided-demo/<scenario_slug>/start")
def start_guided_demo(scenario_slug: str):
    try:
        return_case = guided_demo.start(scenario_slug)
    except KeyError:
        return jsonify({"errore": "Demo guidata non trovata."}), 404
    except Exception:  # noqa: BLE001 - non esporre dettagli interni nella demo pubblica
        logger.exception("Impossibile avviare la demo guidata")
        return jsonify({"errore": "Impossibile preparare la demo guidata."}), 500
    return jsonify(guided_demo.payload(return_case))


@app.post("/api/guided-demo/cases/<case_id>/next")
def advance_guided_demo(case_id: str):
    try:
        return_case = guided_demo.advance(case_id)
    except KeyError:
        return jsonify({"errore": "Pratica guidata non trovata."}), 404
    except (ValueError, domain.InvalidTransition) as exc:
        return jsonify({"errore": str(exc)}), 409
    except Exception:  # noqa: BLE001
        logger.exception("Impossibile avanzare la demo guidata")
        return jsonify({"errore": "Impossibile avanzare la demo guidata."}), 500
    return jsonify(guided_demo.payload(return_case))


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
@app.get("/cases")
def database_register():
    support_copilot.ensure_demo_cases()
    legacy_alias = request.path == "/database"
    filters = {
        "status": (request.args.get("status") or "").strip() or None,
        "reason": (request.args.get("reason") or "").strip() or None,
        "workflow_key": (request.args.get("workflow") or "").strip() or None,
        "query": (request.args.get("q") or "").strip() or None,
    }
    source_prefix = None if legacy_alias else "policy_copilot"
    all_cases = database.list_cases(source_mode_prefix=source_prefix, limit=500)
    cases = database.list_cases(
        **filters, source_mode_prefix=source_prefix, limit=500
    )
    counts = database.message_counts([item["id"] for item in cases])
    for item in cases:
        item["message_count"] = counts.get(item["id"], 0)
        item["rule_id"] = (item.get("policy_decision") or {}).get("rule_id")
    return render_template(
        "database.html",
        cases=cases,
        metrics={
            "total": len(all_cases),
            "open": sum(item["status"] != domain.CaseStatus.CLOSED.value for item in all_cases),
            "closed": sum(item["status"] == domain.CaseStatus.CLOSED.value for item in all_cases),
            "escalated": sum(
                item["status"] == domain.CaseStatus.ESCALATED.value
                or item.get("actual_outcome") == "escalation"
                for item in all_cases
            ),
            "messages": sum(database.message_counts([item["id"] for item in all_cases]).values()),
        },
        filters=filters,
        statuses=[status.value for status in domain.CaseStatus],
        status_labels=domain.STATUS_LABELS,
        reasons=sorted({case["return_reason"] for case in all_cases}),
        category_labels=support_copilot.CATEGORY_LABELS,
        workflow_labels={key: value["label"] for key, value in support_copilot.WORKFLOWS.items()},
        outcome_labels=support_copilot.OUTCOME_LABELS,
        workflows=support_copilot.WORKFLOWS,
        legacy_alias=legacy_alias,
        message_total=sum(counts.values()),
    )


def _policy_rule_count(value) -> int:
    if isinstance(value, dict):
        return sum(_policy_rule_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_policy_rule_count(item) for item in value)
    return 1


@app.get("/policies")
@app.get("/playbooks", endpoint="playbooks")
def policies():
    support_copilot.ensure_demo_cases()
    policy = policy_config.load_policy()
    analytics_metrics = database.copilot_analytics()
    builtins = {
        "customer_care": {
            "title": "Customer Care & Resi",
            "description": "Assistenza, garanzia, recesso, spedizioni e pagamenti",
            "source": policy["source"],
            "version": policy["version"],
            "overview": "Playbook per classificare le richieste clienti e preparare risposte verificabili.",
            "rules": [
                {"id": "RET-01", "label": "Finestra recesso", "value": f"{policy['withdrawal']['window_days']} giorni dalla consegna"},
                {"id": "RET-02", "label": "Condizioni prodotto", "value": "Integro e rivendibile"},
                {"id": "GAR-01", "label": "Finestra garanzia", "value": f"{policy['defective_product']['warranty_max_days']} giorni"},
                {"id": "GAR-02", "label": "Prove difetto", "value": ", ".join(policy['defective_product']['evidence_required'])},
                {"id": "ESC-01", "label": "Controllo umano", "value": "Obbligatorio prima dell’uso della risposta"},
            ],
            "exceptions": policy["unresolved"],
            "flow": [("Richiesta", "Intento cliente"), ("Contesto", "Ordine e fatti"), ("Regole", "Idoneità"), ("Human gate", "Risposta")],
        },
        "agency_ops": {
            "title": "Agency Delivery",
            "description": "Brief, cambi di scope, approvazioni e blocchi di delivery",
            "source": "playbooks/agency_delivery.md",
            "version": "1.0",
            "overview": "Playbook per trasformare comunicazioni di clienti e team in brief e prossime azioni.",
            "rules": [
                {"id": "AGY-01", "label": "Brief minimo", "value": "Obiettivo e scope devono essere espliciti"},
                {"id": "AGY-02", "label": "Fattibilità", "value": "Scadenza e copertura economica verificate"},
                {"id": "CHG-01", "label": "Cambio di scope", "value": "Stimare sempre impatto su tempi e costi"},
                {"id": "APR-01", "label": "Approvazione", "value": "Richiedere un esito esplicito del referente"},
                {"id": "BLK-01", "label": "Blocco critico", "value": "Owner obbligatorio ed escalation delivery"},
            ],
            "exceptions": [{"id": "AGY-X1", "description": "Richieste urgenti senza budget confermato restano sotto revisione del responsabile."}],
            "flow": [("Richiesta", "Brief o change"), ("Contesto", "Scope e vincoli"), ("Impatto", "Tempi e costi"), ("Human gate", "Handoff")],
        },
        "internal_ops": {
            "title": "Internal Operations",
            "description": "Acquisti, accessi, incidenti ed eccezioni interne",
            "source": "playbooks/internal_operations.md",
            "version": "1.0",
            "overview": "Playbook per rendere tracciabili richieste interne, approvazioni e responsabilità.",
            "rules": [
                {"id": "PUR-01", "label": "Motivazione acquisto", "value": "Utilizzo e beneficiari devono essere documentati"},
                {"id": "PUR-03", "label": "Approvazione spesa", "value": "Budget e responsabile verificati prima dell’handoff"},
                {"id": "ACC-01", "label": "Accessi", "value": "Sistema, ruolo, durata e motivazione obbligatori"},
                {"id": "INC-01", "label": "Incidente critico", "value": "Escalation se l’attività aziendale è bloccata"},
                {"id": "EXC-02", "label": "Deroga", "value": "Durata, owner e data di revisione obbligatori"},
            ],
            "exceptions": [{"id": "OPS-X1", "description": "Le urgenze non sostituiscono le approvazioni richieste per accessi o spese."}],
            "flow": [("Richiesta", "Acquisto o accesso"), ("Contesto", "Motivo e impatto"), ("Approval", "Responsabile"), ("Human gate", "Handoff")],
        },
    }
    policy_cards = [
        {"id": key, "title": item["title"], "description": item["description"], "status": "Attivo", "source": item["source"], "version": item["version"], "custom": False}
        for key, item in builtins.items()
    ]
    custom_documents = database.list_policy_documents()
    for document in custom_documents:
        policy_cards.append(
            {
                "id": document["id"],
                "title": document["name"],
                "description": "Playbook strutturato e confermato dall’operatore",
                "status": "Pubblicata",
                "source": document["source_label"],
                "version": f"1.{document['version'] - 1}",
                "custom": True,
                "document": document,
            }
        )
    selected_id = request.args.get("playbook") or request.args.get("policy") or "customer_care"
    if selected_id not in {item["id"] for item in policy_cards}:
        selected_id = "customer_care"
    selected = next(item for item in policy_cards if item["id"] == selected_id)
    selected_exceptions = []
    if selected.get("custom"):
        document = selected["document"]
        rules_view = document["rules"]
        selected_exceptions = [
            {"id": f"REV-{index + 1:02}", "description": copy}
            for index, copy in enumerate(document["confirmations"])
        ]
    else:
        definition = builtins[selected_id]
        rules_view = definition["rules"]
        selected_exceptions = definition["exceptions"]
        description = definition["overview"]
        decision_flow = definition["flow"]
    return render_template(
        "policies.html",
        policy=policy,
        policy_cards=policy_cards,
        selected=selected,
        selected_id=selected_id,
        rules_view=rules_view,
        decision_flow=decision_flow,
        selected_exceptions=selected_exceptions,
        description=description,
        rule_count=sum(len(item["rules"]) for item in builtins.values()) + sum(len(item["rules"]) for item in custom_documents),
        decision_count=analytics_metrics["total"],
        policy_signals=analytics_metrics["insights"],
        rules_used=analytics_metrics["rules"],
        custom_count=len(custom_documents),
    )


@app.post("/policies/extract")
@app.post("/playbooks/extract")
def extract_policy_preview():
    """Estrae una bozza strutturata senza modificare i playbook attivi."""

    try:
        upload = request.files.get("file")
        if upload and upload.filename:
            text = policy_import.document_text(
                upload.filename,
                upload.read(policy_import.MAX_DOCUMENT_BYTES + 1),
                upload.content_type or "",
            )
            source = upload.filename
        else:
            data = request.get_json(silent=True) or {}
            mode = str(data.get("mode") or "text")
            if mode == "url":
                text, source = policy_import.url_text(str(data.get("url") or ""))
            else:
                text = str(data.get("text") or "").strip()
                source = "Testo incollato"
            if not text:
                raise policy_import.PolicyImportError("Inserisci una procedura da analizzare.")
        extraction = policy_import.extract_structured_rules(text)
    except policy_import.PolicyImportError as exc:
        return jsonify({"errore": str(exc)}), 400
    return jsonify({**extraction, "source": source, "sandbox": True})


@app.post("/policies/publish-preview")
@app.post("/playbooks/publish-preview")
def publish_policy_preview():
    """Salva la versione revisionata; il motore resta protetto dall'human gate."""

    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    rules_preview = data.get("rules") or []
    if not name or not isinstance(rules_preview, list) or not rules_preview:
        return jsonify({"errore": "Nome e regole del playbook sono obbligatori."}), 400
    if not data.get("human_confirmed"):
        return jsonify({"errore": "Conferma la revisione umana prima di pubblicare."}), 400
    document = database.publish_policy_document(
        name[:100],
        rules_preview[:100],
        source_label=str(data.get("source") or "Inserimento operatore")[:200],
        confirmations=(data.get("confirmations") or [])[:30],
        normalized_document=(data.get("normalized_document") or [])[:20],
    )
    return jsonify(
        {
            "status": "published",
            "version": "1.0",
            "policy_name": document["name"],
            "policy_id": document["id"],
            "rule_count": len(rules_preview),
            "redirect": url_for("playbooks", playbook=document["id"]),
            "active_policy_unchanged": True,
        }
    )


def _simulation_category(message: str) -> str:
    lowered = message.casefold()
    if any(term in lowered for term in ("articolo errato", "articolo sbagliato", "prodotto sbagliato")):
        return "articolo_errato"
    if any(term in lowered for term in ("arrivato rotto", "pacco rotto", "danno da trasporto")):
        return "arrivato_rotto"
    if any(term in lowered for term in ("non funziona", "difett", "guasto", "doa")):
        return "doa"
    return "recesso"


@app.post("/policies/simulate")
@app.post("/playbooks/simulate")
def simulate_policy():
    """Esegue scenari espliciti senza leggere store o creare casi."""
    data = request.get_json(silent=True) or {}
    presets = {
        "recesso_ok": ("recesso", {"purchase_verified": True, "delivery_days": 7, "product_condition": "integral"}),
        "recesso_out": ("recesso", {"purchase_verified": True, "delivery_days": 22, "product_condition": "integral"}),
        "doa_ok": ("doa", {"purchase_verified": True, "delivery_days": 120, "evidence_received": True, "serial_verified": True}),
        "doa_missing": ("doa", {"purchase_verified": True, "delivery_days": 120, "evidence_received": False, "serial_verified": False}),
        "shipping_delay": ("spedizione", {"purchase_verified": True, "order_status": "delayed"}),
        "agency_project_ready": ("agency_project", {"scope_clear": True, "deadline_confirmed": True, "budget_status": "approved", "owner_assigned": True}),
        "agency_change_high": ("agency_change", {"impact_level": "high", "deadline_confirmed": True, "budget_status": "approved"}),
        "internal_purchase": ("ops_purchase", {"business_reason_clear": True, "budget_status": "approved", "manager_approval": "approved"}),
        "internal_incident": ("ops_incident", {"urgency": "critical", "incident_impact": "business_blocked", "owner_assigned": False}),
    }
    scenario = str(data.get("scenario") or "")
    legacy_request = not scenario and bool(data.get("order_number"))
    if legacy_request:
        category = _simulation_category(str(data.get("message") or ""))
        scenario = {
            "doa": "doa_ok",
            "spedizione": "shipping_delay",
        }.get(category, "recesso_ok")
    if scenario not in presets:
        return jsonify({"errore": "Seleziona uno scenario valido."}), 400
    category, facts = presets[scenario]
    decision = support_copilot.evaluate(category, facts)
    return jsonify(
        {
            "category": category,
            "category_label": support_copilot.CATEGORY_LABELS[category],
            "outcome": decision["outcome"],
            "eligibility": decision["eligibility"],
            "motivation": decision["motivation"],
            "rule_id": (
                "withdrawal_eligible"
                if legacy_request and category == "recesso"
                else decision.get("rule_id")
            ),
            "policy_sections": decision.get("policy_sections") or [],
            "next_action": decision.get("next_action"),
            "no_real_action": True,
        }
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
    support_copilot.ensure_demo_cases()
    metrics = database.copilot_analytics()
    feedback_labels = {
        "policy_interpretation": "Interpretazione policy",
        "missing_information": "Informazioni mancanti",
        "tone_style": "Tono e stile",
        "too_verbose": "Troppo prolisso",
        "incorrect_action": "Azione non corretta",
        "other": "Altro",
    }
    fact_labels = {
        key: value["label"] for key, value in support_copilot.FACTS.items()
    }
    return render_template(
        "analytics.html",
        metrics=metrics,
        category_labels=support_copilot.CATEGORY_LABELS,
        workflow_labels={key: value["label"] for key, value in support_copilot.WORKFLOWS.items()},
        outcome_labels=support_copilot.OUTCOME_LABELS,
        fact_labels=fact_labels,
        feedback_labels=feedback_labels,
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


def _render_workbench(case_id: str | None = None, workflow_key: str | None = None):
    support_copilot.ensure_demo_cases()
    return_case = database.get_case(case_id) if case_id else None
    if case_id and return_case is None:
        return render_template("404.html"), 404
    recent_cases = [
        item for item in database.list_cases(source_mode_prefix="policy_copilot", limit=30)
    ][:6]
    selected_workflow = workflow_key if workflow_key in support_copilot.WORKFLOWS else "customer_care"
    return render_template(
        "workbench.html",
        **support_copilot.view_model(return_case),
        messages=database.get_case_messages(case_id) if case_id else [],
        timeline=database.get_timeline(case_id) if case_id else [],
        recent_cases=recent_cases,
        status_labels=domain.STATUS_LABELS,
        category_labels=support_copilot.CATEGORY_LABELS,
        workflows=support_copilot.WORKFLOWS,
        selected_workflow=selected_workflow,
    )


@app.get("/workbench")
def workbench():
    requested = (request.args.get("case_id") or "").strip()
    if requested and database.get_case(requested):
        return redirect(url_for("workbench_case", case_id=requested))
    return _render_workbench(workflow_key=(request.args.get("workflow") or "").strip())


@app.get("/workbench/<case_id>")
def workbench_case(case_id: str):
    return _render_workbench(case_id)


@app.post("/api/workbench/analyze")
def analyze_support_message():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"errore": "Incolla una comunicazione da analizzare."}), 400
    if len(message) > MAX_MESSAGE_LENGTH:
        return jsonify({"errore": "Il messaggio supera il limite di 5.000 caratteri."}), 400
    try:
        return_case = support_copilot.create_case(
            message,
            workflow_key=str(data.get("workflow") or "customer_care"),
        )
    except ValueError as exc:
        return jsonify({"errore": str(exc)}), 400
    except Exception:  # noqa: BLE001
        logger.exception("Creazione del caso copilot non riuscita")
        return jsonify({"errore": "Non è stato possibile analizzare la comunicazione."}), 500
    return jsonify({"ok": True, "case_id": return_case["id"], "redirect": url_for("workbench_case", case_id=return_case["id"])})


@app.post("/api/workbench/cases/<case_id>/facts")
def record_support_fact(case_id: str):
    data = request.get_json(silent=True) or {}
    try:
        return_case = support_copilot.update_fact(case_id, str(data.get("field") or ""), data.get("value"))
    except KeyError:
        return jsonify({"errore": "Caso non trovato."}), 404
    except ValueError as exc:
        return jsonify({"errore": str(exc)}), 400
    return jsonify({"ok": True, "case": return_case})


@app.post("/api/workbench/cases/<case_id>/messages")
def append_support_message(case_id: str):
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "").strip()
    if not message or len(message) > MAX_MESSAGE_LENGTH:
        return jsonify({"errore": "Scrivi una comunicazione valida."}), 400
    try:
        return_case = support_copilot.add_customer_message(case_id, message)
    except KeyError:
        return jsonify({"errore": "Caso non trovato."}), 404
    return jsonify({"ok": True, "case": return_case})


@app.post("/api/workbench/cases/<case_id>/outcome")
def record_support_outcome(case_id: str):
    data = request.get_json(silent=True) or {}
    try:
        return_case = support_copilot.record_outcome(
            case_id,
            str(data.get("outcome") or ""),
            str(data.get("response") or ""),
            modified=bool(data.get("modified")),
            reason=str(data.get("reason") or "")[:120],
        )
    except KeyError:
        return jsonify({"errore": "Caso non trovato."}), 404
    except ValueError as exc:
        return jsonify({"errore": str(exc)}), 400
    return jsonify({"ok": True, "case": return_case})


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
