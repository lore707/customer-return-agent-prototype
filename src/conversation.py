"""Livello conversazione (multi-battuta) davanti al motore deterministico.

Idea di fondo: la conversazione serve a RIEMPIRE I BUCHI di uno stato esplicito
del caso (categoria + numero_ordine). Finché mancano informazioni, l'agente le
chiede da solo. Quando lo stato è completo, e SOLO allora, aziona il motore già
esistente (shopify_client + tracking + rules via agent.costruisci_pacchetto).

La parte deterministica (rules, shopify_client, tracking) NON viene toccata:
questo modulo è solo l'imbuto che la alimenta.

Stato del caso mantenuto per ogni sessione:
    {
      "categoria": "recesso" | "doa" | ... | None,
      "numero_ordine": "1002" | None,
      "prove_richieste": bool,
      "prove_fornite": bool,
      "sigillo_integro": bool | None,
      "confidence": float,
      "fase": "raccolta" | "in_attesa_operatore" | "chiuso"
    }
"""

import json
import os

import anthropic
from dotenv import load_dotenv

import agent
import classifier
import shopify_client

MODEL = classifier.MODEL
CATEGORIE = classifier.CATEGORIE

# Etichette leggibili per la categoria (usate nelle risposte al cliente).
ETICHETTA_CATEGORIA = {
    "recesso": "reso (ripensamento)",
    "doa": "prodotto difettoso",
    "arrivato_rotto": "prodotto danneggiato in transito",
    "articolo_errato": "articolo errato",
    "altro": "richiesta",
}

# Store in memoria: session_id -> {"stato": {...}, "history": [...]}.
# NB: è un dizionario in RAM (va bene per la demo). Se il server si riavvia,
# lo stato si perde: per un uso reale servirebbe persistenza.
_SESSIONI: dict[str, dict] = {}


PROMPT_ESTRAZIONE = """Sei un estrattore di informazioni per un customer care \
e-commerce. Ti viene data la sequenza dei MESSAGGI DEL CLIENTE in una \
conversazione. Estrai, considerando TUTTA la conversazione (non solo l'ultimo \
messaggio):

- "categoria": una tra recesso | doa | arrivato_rotto | articolo_errato | altro, \
oppure null se non è ancora determinabile.
- "numero_ordine": la stringa del numero d'ordine se il cliente l'ha fornito in \
QUALSIASI punto della conversazione (solo le cifre, es. "1002"), altrimenti null.
- "sigillo_integro": true se il cliente dice esplicitamente che confezione e \
sigillo sono integri, false se dice di aver aperto o usato il prodotto, altrimenti null.
- "requested_resolution": "refund" se chiede esplicitamente un rimborso, "swap" \
se chiede esplicitamente sostituzione/cambio, altrimenti null.
- "escalation_reason": legal_threat | chargeback | consumer_complaint | \
policy_exception | decision_dispute | potential_fraud | ambiguous_evidence | \
multiple_orders | product_not_identified | null.
- "confidence": numero tra 0 e 1 che indica quanto è sicura la classificazione.

Guida categoria:
- recesso: ripensamento/cambio idea; INCLUDE una richiesta generica di reso o \
restituzione ("voglio fare un reso", "vorrei restituire") quando NON vengono \
menzionati difetti, guasti o danni.
- doa: prodotto difettoso/malfunzionante (non si accende, guasto).
- arrivato_rotto: danno da trasporto, arrivato rotto all'apertura.
- articolo_errato: ricevuto un articolo diverso da quello ordinato.
- altro: minacce legali, chargeback, reclami, o casi fuori dai precedenti.

Imposta categoria = null SOLO se il cliente non ha ancora espresso alcuna \
intenzione (né reso, né problema): ad esempio un semplice saluto o "ho un \
problema" senza dettagli. Se ha chiesto un reso/restituzione, è recesso.
Restituisci SOLO un oggetto JSON: {"categoria": ..., "numero_ordine": ..., \
"sigillo_integro": ..., "requested_resolution": ..., "escalation_reason": ..., \
"confidence": ...}"""


def _crea_client() -> anthropic.Anthropic:
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY mancante nel file .env.")
    return anthropic.Anthropic(api_key=api_key)


def _stato_iniziale() -> dict:
    return {
        "categoria": None,
        "numero_ordine": None,
        "prove_richieste": False,
        "prove_fornite": False,
        "sigillo_integro": None,
        "requested_resolution": None,
        "escalation_reason": None,
        "confidence": None,
        "fase": "raccolta",
    }


def get_sessione(session_id: str) -> dict:
    """Recupera (o crea) lo stato di sessione per il session_id dato."""
    if session_id not in _SESSIONI:
        _SESSIONI[session_id] = {"stato": _stato_iniziale(), "history": []}
    return _SESSIONI[session_id]


def reset_sessione(session_id: str) -> None:
    """Elimina lo stato di sessione (usato da 'Nuova conversazione')."""
    _SESSIONI.pop(session_id, None)


def ripristina_sessione(session_id: str, messaggi: list[dict]) -> None:
    """Ricostruisce la memoria in RAM partendo dai messaggi salvati in SQLite.

    Lo stato strutturato viene ricalcolato dal modello al messaggio successivo;
    qui conserviamo soltanto la cronologia leggibile, senza chain-of-thought.
    """
    history = []
    for message in messaggi:
        role = message.get("role") or message.get("ruolo")
        text = message.get("message") or message.get("testo")
        if role in {"cliente", "agente", "operatore", "sistema"} and text:
            history.append({"ruolo": role, "testo": str(text)})
    _SESSIONI[session_id] = {"stato": _stato_iniziale(), "history": history}


def trascrizione_cliente(session_id: str) -> str:
    """Restituisce i messaggi cliente della sessione in ordine cronologico."""
    sessione = _SESSIONI.get(session_id) or {}
    history = sessione.get("history", [])
    return "\n".join(
        item["testo"] for item in history if item.get("ruolo") == "cliente"
    )


def _estrai_stato(history: list[dict]) -> dict:
    """Estrae i dati strutturati da tutti i messaggi del cliente."""
    messaggi_cliente = [h["testo"] for h in history if h["ruolo"] == "cliente"]
    trascrizione = "\n".join(
        f"[{i + 1}] {t}" for i, t in enumerate(messaggi_cliente)
    )

    client = _crea_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=256,
        system=PROMPT_ESTRAZIONE,
        messages=[
            {
                "role": "user",
                "content": f"MESSAGGI DEL CLIENTE:\n{trascrizione}",
            }
        ],
    )
    testo = "".join(b.text for b in response.content if b.type == "text")
    dati = classifier.estrai_json(testo)  # riusa il parser robusto del classifier

    categoria = dati.get("categoria")
    if categoria not in CATEGORIE:
        categoria = None

    numero = dati.get("numero_ordine")
    if numero is not None:
        numero = str(numero).strip().lstrip("#").strip() or None

    sigillo = dati.get("sigillo_integro")
    if not isinstance(sigillo, bool):
        sigillo = None

    requested_resolution = dati.get("requested_resolution")
    if requested_resolution not in {"refund", "swap"}:
        requested_resolution = None

    escalation_reason = dati.get("escalation_reason")
    if escalation_reason not in {
        "legal_threat", "chargeback", "consumer_complaint", "policy_exception",
        "decision_dispute", "potential_fraud", "ambiguous_evidence",
        "multiple_orders", "product_not_identified",
    }:
        escalation_reason = None

    try:
        confidence = float(dati.get("confidence"))
    except (TypeError, ValueError):
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))

    return {
        "categoria": categoria,
        "numero_ordine": numero,
        "sigillo_integro": sigillo,
        "requested_resolution": requested_resolution,
        "escalation_reason": escalation_reason,
        "confidence": confidence,
    }


def _aggiorna_stato(stato: dict, estratto: dict) -> None:
    """Aggiorna lo stato: un valore non-null vince sempre, un null non sovrascrive."""
    for campo in (
        "categoria", "numero_ordine", "sigillo_integro", "requested_resolution",
        "escalation_reason", "confidence",
    ):
        if estratto.get(campo) is not None:
            stato[campo] = estratto[campo]


# --- Messaggi interlocutori dell'agente (template: richieste di informazioni) ---
# Sono richieste di dati, non decisioni: l'agente le manda da solo. La risposta
# RISOLUTIVA resta invece soggetta all'approvazione dell'operatore.

def _chiedi_numero(categoria: str | None) -> str:
    if categoria and categoria in ETICHETTA_CATEGORIA and categoria != "altro":
        apertura = (
            f"Certo, mi occupo subito della sua richiesta di "
            f"{ETICHETTA_CATEGORIA[categoria]}."
        )
    else:
        apertura = "Certo, la aiuto volentieri."
    return (
        f"{apertura} Per procedere ho bisogno del numero del suo ordine: "
        "può indicarmelo? Di solito lo trova nell'e-mail di conferma dell'ordine."
    )


def _chiedi_chiarimento() -> str:
    return (
        "Mi può descrivere meglio il problema, così la indirizzo correttamente? "
        "Ad esempio: desidera restituire un prodotto per ripensamento, il "
        "prodotto è difettoso, è arrivato danneggiato, oppure ha ricevuto "
        "l'articolo sbagliato?"
    )


def _numero_non_valido(numero: str) -> str:
    return (
        f"Mi dispiace, non trovo un ordine con il numero {numero} nel nostro "
        "sistema. Può verificare il numero e reinviarmelo? Lo trova nell'e-mail "
        "di conferma dell'ordine."
    )


def processa_messaggio(session_id: str, testo_cliente: str) -> dict:
    """Elabora un messaggio del cliente e fa avanzare la conversazione.

    Ritorna un dict con:
        session_id, stato, tipo ("richiesta_info" | "pacchetto"),
        e — a seconda del tipo — risposta_agente oppure pacchetto.
    """
    sessione = get_sessione(session_id)
    stato = sessione["stato"]
    history = sessione["history"]

    history.append({"ruolo": "cliente", "testo": testo_cliente})

    # 1) Aggiorna lo stato estraendo da TUTTA la conversazione.
    estratto = _estrai_stato(history)
    _aggiorna_stato(stato, estratto)

    def _risposta_info(testo: str) -> dict:
        history.append({"ruolo": "agente", "testo": testo})
        stato["fase"] = "raccolta"
        return {
            "session_id": session_id,
            "stato": dict(stato),
            "tipo": "richiesta_info",
            "risposta_agente": testo,
        }

    # 2) Manca il numero d'ordine → l'agente lo chiede (nessuna verifica).
    if stato["numero_ordine"] is None:
        return _risposta_info(_chiedi_numero(stato["categoria"]))

    # 3) Manca la categoria ma c'è il numero → chiedi chiarimento.
    if stato["categoria"] is None:
        return _risposta_info(_chiedi_chiarimento())

    # 4) Entrambi presenti → valida l'esistenza dell'ordine, poi aziona il motore.
    try:
        dati_ordine = shopify_client.get_order(stato["numero_ordine"])
        nota_tecnica = None
    except shopify_client.ShopifyError as exc:
        # Errore tecnico: non è "numero inesistente"; proseguiamo verso il
        # pacchetto (il motore produrrà un'escalation con nota tecnica).
        dati_ordine = None
        nota_tecnica = f"Errore Shopify: {exc}"

    # Numero inesistente (ordine non trovato e nessun errore tecnico): chiedi di nuovo.
    if dati_ordine is None and nota_tecnica is None:
        numero_errato = stato["numero_ordine"]
        stato["numero_ordine"] = None  # svuota il campo per raccoglierlo di nuovo
        return _risposta_info(_numero_non_valido(numero_errato))

    # Stato completo e ordine valido → aziona il motore esistente.
    pacchetto = agent.costruisci_pacchetto(
        stato["categoria"],
        stato["numero_ordine"],
        dati_ordine=dati_ordine,
        nota_tecnica=nota_tecnica,
        prove_fornite=stato["prove_fornite"],
        sigillo_integro=stato["sigillo_integro"],
        confidence=stato["confidence"],
        requested_resolution=stato["requested_resolution"],
        escalation_reason=stato["escalation_reason"],
    )

    esito = pacchetto["azione_proposta"]["esito_proposto"]
    if esito == "chiedi_foto_video":
        stato["prove_richieste"] = True

    stato["fase"] = "in_attesa_operatore"
    history.append({"ruolo": "sistema", "testo": f"[pacchetto pronto: {esito}]"})

    return {
        "session_id": session_id,
        "stato": dict(stato),
        "tipo": "pacchetto",
        "pacchetto": pacchetto,
    }


def chiudi_sessione(session_id: str, testo_operatore: str | None = None) -> None:
    """Segna la sessione come chiusa dopo la decisione dell'operatore."""
    sessione = _SESSIONI.get(session_id)
    if not sessione:
        return
    sessione["stato"]["fase"] = "chiuso"
    if testo_operatore:
        sessione["history"].append({"ruolo": "operatore", "testo": testo_operatore})


def riapri_raccolta(session_id: str) -> None:
    """Riporta in raccolta una sessione quando l'operatore chiede altri dati."""
    sessione = _SESSIONI.get(session_id)
    if sessione:
        sessione["stato"]["fase"] = "raccolta"


def segna_prove_fornite(session_id: str) -> None:
    """Segna le prove come ricevute solo dopo una conferma esplicita."""
    sessione = get_sessione(session_id)
    sessione["stato"]["prove_fornite"] = True
    sessione["stato"]["fase"] = "raccolta"
