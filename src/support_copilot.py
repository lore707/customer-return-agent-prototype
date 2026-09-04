"""Motore per richieste operative senza integrazioni esterne.

Ogni workflow definisce intenti, fatti necessari, playbook e possibili esiti.
Il motore rimuove gli identificatori più comuni, struttura la richiesta e
applica regole deterministiche prima di proporre una prossima azione.
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
    "agency_project": "Nuovo progetto",
    "agency_change": "Cambio di scope",
    "agency_approval": "Approvazione cliente",
    "agency_blocker": "Blocco di delivery",
    "ops_purchase": "Richiesta di acquisto",
    "ops_access": "Richiesta di accesso",
    "ops_incident": "Incidente operativo",
    "ops_exception": "Eccezione di processo",
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
    "scope_clear": {
        "label": "Scope definito",
        "question": "Obiettivo e perimetro della richiesta sono chiari?",
        "type": "choice",
        "options": [("true", "Sì, sono chiari"), ("false", "No, manca un brief")],
    },
    "deadline_confirmed": {
        "label": "Scadenza confermata",
        "question": "La scadenza è stata verificata e confermata?",
        "type": "choice",
        "options": [("true", "Sì"), ("false", "Non ancora")],
    },
    "budget_status": {
        "label": "Copertura economica",
        "question": "Qual è lo stato del budget o della copertura economica?",
        "type": "choice",
        "options": [("approved", "Approvato"), ("pending", "Da approvare"), ("not_required", "Non richiesto")],
    },
    "owner_assigned": {
        "label": "Responsabile assegnato",
        "question": "È già stato identificato un responsabile del lavoro?",
        "type": "choice",
        "options": [("true", "Sì"), ("false", "No")],
    },
    "impact_level": {
        "label": "Impatto sul piano",
        "question": "Quanto incide la richiesta su tempi, costi o attività concordate?",
        "type": "choice",
        "options": [("low", "Basso"), ("medium", "Medio"), ("high", "Alto")],
    },
    "approval_status": {
        "label": "Stato approvazione",
        "question": "Qual è lo stato dell’approvazione del referente?",
        "type": "choice",
        "options": [("approved", "Approvato"), ("changes", "Modifiche richieste"), ("pending", "In attesa")],
    },
    "business_reason_clear": {
        "label": "Motivazione documentata",
        "question": "La necessità operativa è descritta in modo sufficiente?",
        "type": "choice",
        "options": [("true", "Sì"), ("false", "No")],
    },
    "manager_approval": {
        "label": "Approvazione responsabile",
        "question": "Qual è lo stato dell’approvazione del responsabile?",
        "type": "choice",
        "options": [("approved", "Approvata"), ("pending", "Da richiedere"), ("rejected", "Rifiutata"), ("not_required", "Non richiesta")],
    },
    "urgency": {
        "label": "Urgenza",
        "question": "Qual è il livello di urgenza verificato?",
        "type": "choice",
        "options": [("normal", "Normale"), ("high", "Alta"), ("critical", "Critica")],
    },
    "incident_impact": {
        "label": "Impatto dell’incidente",
        "question": "Qual è l’impatto operativo osservato?",
        "type": "choice",
        "options": [("limited", "Limitato"), ("multiple_people", "Più persone coinvolte"), ("business_blocked", "Attività bloccata")],
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
    "agency_project": ["scope_clear", "deadline_confirmed", "budget_status", "owner_assigned"],
    "agency_change": ["impact_level", "deadline_confirmed", "budget_status"],
    "agency_approval": ["approval_status", "owner_assigned"],
    "agency_blocker": ["impact_level", "owner_assigned"],
    "ops_purchase": ["business_reason_clear", "budget_status", "manager_approval"],
    "ops_access": ["business_reason_clear", "manager_approval", "owner_assigned"],
    "ops_incident": ["urgency", "incident_impact", "owner_assigned"],
    "ops_exception": ["business_reason_clear", "manager_approval"],
}

OUTCOME_LABELS = {
    "informazioni_richieste": "Informazioni richieste",
    "risposta_inviata": "Risposta inviata",
    "rimborso": "Rimborso",
    "swap": "Sostituzione",
    "respinto": "Richiesta respinta",
    "escalation": "Escalation",
    "risolto": "Risolto",
    "brief_creato": "Brief creato",
    "proposta_inviata": "Proposta inviata",
    "approvato": "Approvato",
    "rifiutato": "Rifiutato",
    "assegnato": "Assegnato",
    "completato": "Completato",
}

WORKFLOW_OUTCOMES = {
    "customer_care": ["informazioni_richieste", "risposta_inviata", "rimborso", "swap", "respinto", "escalation", "risolto"],
    "agency_ops": ["informazioni_richieste", "brief_creato", "proposta_inviata", "approvato", "rifiutato", "escalation", "completato"],
    "internal_ops": ["informazioni_richieste", "assegnato", "approvato", "rifiutato", "escalation", "completato"],
}

WORKFLOWS = {
    "customer_care": {
        "label": "Customer care",
        "short": "Assistenza e resi",
        "description": "Richieste clienti, garanzie, spedizioni e pagamenti.",
        "input_label": "Comunicazione cliente",
        "output_label": "Risposta da revisionare",
        "playbook": "Customer Care & Resi",
        "examples": [
            ("Prodotto difettoso", "Il prodotto non si accende più e vorrei capire come usare la garanzia."),
            ("Recesso", "Ho cambiato idea e vorrei restituire il prodotto che ho ricevuto."),
            ("Spedizione", "Il tracking dice consegnato ma io non ho ricevuto il pacco."),
        ],
    },
    "agency_ops": {
        "label": "Agenzia & delivery",
        "short": "Brief e cambi di scope",
        "description": "Nuovi progetti, change request, approvazioni e blocchi.",
        "input_label": "Richiesta del cliente o del team",
        "output_label": "Brief e prossima azione",
        "playbook": "Agency Delivery",
        "examples": [
            ("Nuovo progetto", "Il cliente chiede una landing page per il lancio di ottobre e vorrebbe partire subito."),
            ("Cambio di scope", "Il cliente vuole aggiungere una seconda lingua al sito già approvato senza spostare la consegna."),
            ("Blocco delivery", "Il team non può procedere perché mancano gli asset definitivi del cliente."),
        ],
    },
    "internal_ops": {
        "label": "Operations interne",
        "short": "Richieste e approvazioni",
        "description": "Acquisti, accessi, incidenti ed eccezioni di processo.",
        "input_label": "Richiesta interna",
        "output_label": "Nota operativa e handoff",
        "playbook": "Internal Operations",
        "examples": [
            ("Acquisto", "Serve acquistare tre nuove licenze software per il team commerciale."),
            ("Accesso", "Una nuova collega deve accedere al gestionale prima dell’onboarding di lunedì."),
            ("Incidente", "Il sistema di reportistica è bloccato e tutto il team finance non riesce a lavorare."),
        ],
    },
}

CATEGORY_WORKFLOW = {
    **{key: "customer_care" for key in ("recesso", "doa", "arrivato_rotto", "articolo_errato", "spedizione", "pagamento", "informazioni_prodotto", "reclamo", "altro")},
    **{key: "agency_ops" for key in ("agency_project", "agency_change", "agency_approval", "agency_blocker")},
    **{key: "internal_ops" for key in ("ops_purchase", "ops_access", "ops_incident", "ops_exception")},
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


def classify(message: str, workflow_key: str = "customer_care") -> dict:
    lowered = message.casefold()
    if workflow_key == "agency_ops":
        groups = [
            ("agency_change", ("aggiungere", "modifica", "cambio", "scope", "extra", "variante", "seconda lingua")),
            ("agency_approval", ("approvazione", "approvare", "feedback finale", "via libera", "conferma cliente")),
            ("agency_blocker", ("blocc", "mancano gli asset", "non possiamo procedere", "in ritardo", "dipendenza")),
            ("agency_project", ("nuovo progetto", "landing", "campagna", "sito", "preventivo", "brief", "lancio")),
        ]
        for category, terms in groups:
            if any(term in lowered for term in terms):
                return {"category": category, "confidence": 0.92, "label": CATEGORY_LABELS[category]}
        return {"category": "agency_project", "confidence": 0.66, "label": CATEGORY_LABELS["agency_project"]}
    if workflow_key == "internal_ops":
        groups = [
            ("ops_incident", ("blocc", "incidente", "errore", "non funziona", "fermo", "down", "problema urgente")),
            ("ops_access", ("accesso", "permesso", "account", "credenzial", "abilitare", "onboarding")),
            ("ops_purchase", ("acquist", "licenz", "fornitore", "spesa", "budget", "ordine interno")),
            ("ops_exception", ("eccezione", "deroga", "fuori processo", "saltare", "procedura speciale")),
        ]
        for category, terms in groups:
            if any(term in lowered for term in terms):
                return {"category": category, "confidence": 0.92, "label": CATEGORY_LABELS[category]}
        return {"category": "ops_exception", "confidence": 0.64, "label": CATEGORY_LABELS["ops_exception"]}
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

    if category == "agency_project":
        if not facts["scope_clear"]:
            return _result("needs_information", "raccogli_brief", "AGY-01", "Obiettivo e perimetro non sono ancora abbastanza chiari.", "Preparare le domande per completare il brief.", "Ciao, per trasformare la richiesta in un brief operativo ci servono obiettivo, deliverable attesi, pubblico e materiali disponibili. Appena li riceviamo possiamo confermare il prossimo passaggio.")
        if not facts["deadline_confirmed"] or facts["budget_status"] == "pending":
            return _result("manual_review", "valuta_fattibilita", "AGY-02", "Scope chiaro, ma tempi o copertura economica richiedono conferma.", "Preparare il brief e sottoporlo al responsabile di delivery.", "Abbiamo strutturato il brief iniziale. Prima di confermare l’avvio dobbiamo validare tempistiche e copertura economica con il responsabile di delivery.")
        if not facts["owner_assigned"]:
            return _result("manual_review", "assegna_owner", "AGY-03", "La richiesta è completa ma non ha ancora un responsabile.", "Assegnare un owner prima del kickoff.", "La richiesta è completa e pronta per la pianificazione. Stiamo assegnando il responsabile che confermerà kickoff e prossimi passaggi.")
        return _result("eligible", "crea_brief", "AGY-04", "Scope, scadenza, copertura e responsabilità sono verificati.", "Creare il brief e preparare il kickoff.", "Abbiamo verificato le informazioni: la richiesta è pronta per essere trasformata in brief operativo e pianificata con il team.")

    if category == "agency_change":
        if not facts["deadline_confirmed"] or facts["budget_status"] == "pending":
            return _result("manual_review", "valuta_change_request", "CHG-01", "La modifica può incidere sul piano e richiede una nuova valutazione.", "Stimare impatto su tempi, costi e attività prima di confermare.", "Abbiamo registrato la modifica richiesta. Prima di confermarla valuteremo l’impatto su tempi, costi e attività già pianificate.")
        if facts["impact_level"] == "high":
            return _result("manual_review", "approvazione_change", "CHG-02", "L’impatto stimato è alto e supera il percorso standard.", "Sottoporre la change request al responsabile di delivery.", "La modifica ha un impatto rilevante sul piano approvato. La sottoponiamo al responsabile di delivery prima di aggiornare la pianificazione.")
        return _result("eligible", "aggiorna_piano", "CHG-03", "Impatto, scadenza e copertura sono stati verificati.", "Aggiornare brief e piano di lavoro.", "La modifica è stata valutata e può essere inserita nel piano. Condivideremo il brief aggiornato prima dell’esecuzione.")

    if category == "agency_approval":
        if facts["approval_status"] == "pending":
            return _result("needs_information", "sollecita_approvazione", "APR-01", "Il referente non ha ancora espresso un esito.", "Inviare un riepilogo con una richiesta di approvazione esplicita.", "Per procedere abbiamo bisogno di una conferma esplicita sul materiale condiviso. Puoi approvarlo oppure indicarci le modifiche necessarie?")
        if facts["approval_status"] == "changes":
            return _result("manual_review", "registra_feedback", "APR-02", "Sono state richieste modifiche che devono essere strutturate.", "Trasformare il feedback in attività e assegnarlo al responsabile.", "Abbiamo registrato le modifiche richieste. Le stiamo trasformando in attività verificabili e condivideremo il nuovo passaggio di revisione.")
        if not facts["owner_assigned"]:
            return _result("manual_review", "assegna_owner", "APR-03", "L’approvazione è presente ma manca il responsabile dell’handoff.", "Assegnare il responsabile della fase successiva.", "L’approvazione è registrata. Prima di procedere assegniamo il responsabile della fase successiva.")
        return _result("eligible", "procedi_delivery", "APR-04", "Approvazione e responsabilità sono confermate.", "Avviare la fase di delivery prevista.", "L’approvazione è stata registrata e il lavoro può passare alla fase successiva del piano.")

    if category == "agency_blocker":
        if facts["impact_level"] == "high" or not facts["owner_assigned"]:
            return _result("manual_review", "escalation_delivery", "BLK-01", "Il blocco ha impatto alto oppure non ha ancora un responsabile.", "Assegnare un owner ed escalare al responsabile di delivery.", "Abbiamo registrato il blocco e il suo impatto. Il caso viene assegnato al responsabile di delivery per definire una soluzione e aggiornare il piano.")
        return _result("eligible", "piano_sblocco", "BLK-02", "Il blocco è circoscritto e ha un responsabile.", "Registrare azione, owner e nuova data di verifica.", "Il blocco è stato preso in carico. Abbiamo registrato il responsabile e il prossimo controllo sul piano di risoluzione.")

    if category == "ops_purchase":
        if not facts["business_reason_clear"]:
            return _result("needs_information", "completa_motivazione", "PUR-01", "La necessità operativa non è documentata.", "Richiedere utilizzo, beneficiario e motivazione.", "Per valutare la richiesta servono utilizzo previsto, persone coinvolte e motivazione operativa dell’acquisto.")
        if facts["manager_approval"] == "rejected":
            return _result("not_eligible", "richiesta_respinta", "PUR-02", "Il responsabile ha rifiutato la richiesta.", "Registrare il rifiuto e comunicarne la motivazione.", "La richiesta non è stata approvata dal responsabile e non può procedere nel flusso di acquisto.")
        if facts["budget_status"] == "pending" or facts["manager_approval"] == "pending":
            return _result("manual_review", "richiedi_approvazione", "PUR-03", "Budget o approvazione sono ancora sospesi.", "Preparare il riepilogo per l’approvazione.", "La richiesta è stata strutturata ed è pronta per la verifica di budget e l’approvazione del responsabile.")
        return _result("eligible", "avvia_acquisto", "PUR-04", "Motivazione, copertura e approvazione risultano valide.", "Creare l’handoff verso procurement.", "La richiesta contiene le informazioni e le approvazioni necessarie. Può essere inoltrata al processo di acquisto.")

    if category == "ops_access":
        if not facts["business_reason_clear"]:
            return _result("needs_information", "completa_accesso", "ACC-01", "Sistema, ruolo o motivazione dell’accesso non sono definiti.", "Richiedere sistema, ruolo, durata e motivazione.", "Per valutare l’accesso servono sistema interessato, ruolo richiesto, durata e motivazione operativa.")
        if facts["manager_approval"] in {"pending", "rejected"}:
            return _result("manual_review", "verifica_approvazione", "ACC-02", "L’approvazione richiesta non è disponibile.", "Ottenere una conferma valida prima dell’abilitazione.", "La richiesta è stata registrata, ma prima di procedere serve l’approvazione prevista dal playbook degli accessi.")
        if not facts["owner_assigned"]:
            return _result("manual_review", "assegna_system_owner", "ACC-03", "Manca il responsabile autorizzato a eseguire l’abilitazione.", "Assegnare il system owner.", "La richiesta è completa. Stiamo identificando il responsabile autorizzato che potrà eseguire l’abilitazione.")
        return _result("eligible", "abilita_accesso", "ACC-04", "Motivazione, approvazione e responsabile sono verificati.", "Creare l’handoff per l’abilitazione.", "La richiesta di accesso è completa e può essere inoltrata al responsabile del sistema per l’esecuzione.")

    if category == "ops_incident":
        if facts["urgency"] == "critical" or facts["incident_impact"] == "business_blocked":
            return _result("manual_review", "escalation_incidente", "INC-01", "L’incidente è critico o blocca un’attività aziendale.", "Escalare immediatamente e assegnare un incident owner.", "L’incidente è stato classificato come prioritario e deve essere preso in carico immediatamente dal responsabile previsto.")
        if not facts["owner_assigned"]:
            return _result("manual_review", "assegna_incidente", "INC-02", "Impatto verificato, ma manca un responsabile.", "Assegnare un owner e una prossima verifica.", "L’incidente è stato registrato. Serve assegnare il responsabile prima di avviare il piano di risoluzione.")
        return _result("eligible", "avvia_risoluzione", "INC-03", "Impatto e responsabilità sono definiti.", "Avviare il piano operativo e fissare il prossimo controllo.", "L’incidente è stato classificato e assegnato. Il piano di risoluzione può essere avviato con una prossima verifica tracciata.")

    if category == "ops_exception":
        if not facts["business_reason_clear"]:
            return _result("needs_information", "motiva_eccezione", "EXC-01", "La deroga non ha una motivazione verificabile.", "Richiedere motivo, durata e rischio della deroga.", "Per valutare l’eccezione servono motivazione, durata prevista e rischio operativo associato.")
        if facts["manager_approval"] == "approved":
            return _result("eligible", "registra_eccezione", "EXC-02", "La deroga è motivata e approvata.", "Registrare validità, responsabile e data di revisione.", "L’eccezione è stata approvata. Deve essere registrata con durata, responsabile e data di revisione.")
        if facts["manager_approval"] == "rejected":
            return _result("not_eligible", "eccezione_respinta", "EXC-03", "La deroga è stata rifiutata.", "Applicare il processo standard.", "L’eccezione non è stata approvata: la richiesta deve seguire il processo standard.")
        return _result("manual_review", "approva_eccezione", "EXC-04", "La motivazione è presente, ma manca l’approvazione.", "Inviare la deroga al responsabile.", "La richiesta di eccezione è stata strutturata ed è pronta per la valutazione del responsabile.")

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


def create_case(
    message: str,
    *,
    workflow_key: str = "customer_care",
    operator: str = "Operatore demo",
    path=None,
) -> dict:
    if workflow_key not in WORKFLOWS:
        raise ValueError("Workflow non valido.")
    sanitized, redactions = anonymize_message(message)
    classification = classify(sanitized, workflow_key)
    category = classification["category"]
    result = evaluate(category, {})
    case = database.create_case(
        {
            "id": _case_id(),
            "session_id": f"copilot-{uuid.uuid4().hex}",
            "request_date": database.utc_now(),
            "return_type": "operations_case",
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
            "workflow_key": workflow_key,
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
    if outcome in {"rimborso", "swap", "respinto", "risolto", "approvato", "rifiutato", "completato"}:
        if updated["status"] == domain.CaseStatus.WAITING_HUMAN_APPROVAL.value:
            target = domain.CaseStatus.REJECTED.value if outcome in {"respinto", "rifiutato"} else domain.CaseStatus.APPROVED.value
            updated = database.transition_case(case_id, target, event_type="operator_decision_recorded", details={"outcome": outcome}, path=path)
        if updated["status"] in {domain.CaseStatus.APPROVED.value, domain.CaseStatus.REJECTED.value, domain.CaseStatus.NEEDS_INFORMATION.value, domain.CaseStatus.ESCALATED.value}:
            updated = database.transition_case(case_id, domain.CaseStatus.CLOSED.value, event_type="case_closed", details={"outcome": outcome}, path=path)
    return updated


def view_model(case: dict | None) -> dict:
    if not case:
        return {"case": None, "question": None, "workflow": None, "outcome_labels": {}}
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
    workflow_key = case.get("workflow_key") or CATEGORY_WORKFLOW.get(case.get("return_reason"), "customer_care")
    workflow = WORKFLOWS.get(workflow_key, WORKFLOWS["customer_care"])
    outcome_keys = WORKFLOW_OUTCOMES.get(workflow_key, WORKFLOW_OUTCOMES["customer_care"])
    return {
        "case": case,
        "question": question_payload(missing[0]) if missing else None,
        "category_label": CATEGORY_LABELS.get(case.get("return_reason"), "Da classificare"),
        "outcome_labels": {key: OUTCOME_LABELS[key] for key in outcome_keys},
        "workflow": {"key": workflow_key, **workflow},
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
    {
        "slug": "copilot-demo-agency-project",
        "workflow": "agency_ops",
        "message": "Il cliente chiede una landing page per il lancio di ottobre e vorrebbe partire subito.",
        "facts": {"scope_clear": True, "deadline_confirmed": True, "budget_status": "approved", "owner_assigned": True},
        "outcome": "brief_creato",
    },
    {
        "slug": "copilot-demo-agency-change",
        "workflow": "agency_ops",
        "message": "Il cliente vuole aggiungere una seconda lingua al sito senza spostare la consegna.",
        "facts": {"impact_level": "high", "deadline_confirmed": True, "budget_status": "pending"},
        "outcome": "proposta_inviata",
        "modified": True,
        "reason": "policy_interpretation",
    },
    {
        "slug": "copilot-demo-internal-purchase",
        "workflow": "internal_ops",
        "message": "Serve acquistare tre nuove licenze software per il team commerciale.",
        "facts": {"business_reason_clear": True, "budget_status": "approved", "manager_approval": "approved"},
        "outcome": "approvato",
    },
    {
        "slug": "copilot-demo-internal-incident",
        "workflow": "internal_ops",
        "message": "Il sistema di reportistica è bloccato e il team finance non riesce a lavorare.",
        "facts": {"urgency": "critical", "incident_impact": "business_blocked", "owner_assigned": False},
        "outcome": "escalation",
    },
]


def ensure_demo_cases(*, path=None) -> int:
    """Crea una volta un piccolo dataset, sempre esplicitamente marcato come demo."""
    created = 0
    for sample in DEMO_CASES:
        if database.get_case_by_scenario(sample["slug"], path):
            continue
        case = create_case(
            sample["message"],
            workflow_key=sample.get("workflow", "customer_care"),
            operator="Operatore demo",
            path=path,
        )
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
