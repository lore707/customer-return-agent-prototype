"""Orchestratore dell'agente di customer care (MVP).

Mette in fila i moduli esistenti su un ticket reale e produce il "pacchetto
operatore" descritto in policies.md §9:

    ticket -> classifica -> leggi ordine -> leggi consegna -> applica regole
           -> genera bozza di risposta

Separazione di responsabilità (scelta di design):
- Il MOTORE DI REGOLE (rules.py) decide COSA fare: logica deterministica,
  difendibile riga per riga.
- CLAUDE scrive COME dirlo al cliente: solo il linguaggio, mai la decisione.

L'agente NON invia nulla e NON esegue azioni reali su Shopify/Sendcloud/
gestionale: prepara solo il pacchetto per l'operatore, che approva o modifica.

Uso:
    python src/agent.py tickets/ticket05.txt
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import classifier
import rules
import shopify_client
import tracking

# Stesso modello usato per la classificazione (policy: MVP su claude-sonnet-4-6).
MODEL = classifier.MODEL

ROOT = Path(__file__).resolve().parent.parent

# delivery_info "vuoto" quando non c'è un numero d'ordine da interrogare.
DELIVERY_VUOTO = {"tracking": None, "delivered_at": None}


PROMPT_BOZZA = """Sei un assistente di customer care per e-commerce (store Laifen, \
Xiaomi, Yimiki, Dreo). Scrivi in italiano, con tono professionale ma cordiale.

REGOLA FONDAMENTALE: la DECISIONE è già stata presa dal motore di regole. Tu NON \
decidi nulla e NON contraddici l'esito: scrivi solo il testo, coerente con \
l'esito indicato. Non promettere azioni diverse da quelle previste.

A seconda dell'esito, produci:

- chiedi_numero_ordine: e-mail al cliente che chiede gentilmente il numero d'ordine.
- chiedi_foto_video: e-mail che chiede foto e video che dimostrino il problema, \
spiegando che servono per aprire la pratica.
- chiedi_stato_sigillo: e-mail che chiede se confezione e sigillo sono integri e \
se il prodotto è stato aperto o usato.
- procedi_rimborso: e-mail che conferma l'accettazione del reso e segue i VINCOLI \
POLICY ricevuti. Se shipping_payer è "customer", comunica che il costo esatto \
verrà indicato prima della conferma e detratto dal rimborso; se è "company", \
specifica che la spedizione è a carico dell'azienda. Includi le istruzioni \
sull'imballo esterno solo quando presenti. Il rimborso parte sempre dopo rientro \
e verifica fisica quando physical_validation_required è true.
- procedi_swap: e-mail che conferma la sostituzione (swap) del dispositivo; lo swap \
parte dopo il rientro e la verifica del reso. Specifica il pagatore della \
spedizione indicato nei VINCOLI POLICY e non promettere un modello non confermato.
- offri_scelta_rimborso_o_swap: e-mail che chiede al cliente se preferisce il \
rimborso oppure la sostituzione (swap).
- rifiuta_recesso_prodotto_escluso: e-mail che spiega con garbo che rasoi e \
spazzolini, una volta aperti, non sono ammessi al recesso perché non più \
rivendibili; precisando che un eventuale difetto resta comunque coperto dalla garanzia.
- rifiuta_fuori_finestra: e-mail che spiega con garbo che i 14 giorni per il recesso, \
decorrenti dalla consegna, sono trascorsi.
- ordine_non_consegnato: e-mail che informa il cliente che l'ordine non risulta \
ancora consegnato e che verrà verificato lo stato della spedizione.
- escalation_operatore: NON scrivere al cliente. Scrivi invece una NOTA INTERNA per \
l'operatore umano che riassume il caso e perché richiede intervento manuale. \
Inizia il testo con "[NOTA INTERNA OPERATORE]".

Produci SOLO il testo della e-mail (o della nota interna), senza oggetto, senza \
intestazioni tecniche, senza spiegazioni aggiuntive."""


def _crea_client() -> anthropic.Anthropic:
    """Crea il client Anthropic leggendo la chiave da .env."""
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY mancante. Inseriscila nel file .env."
        )
    return anthropic.Anthropic(api_key=api_key)


def _formatta_prodotti(dati_ordine: dict | None) -> str:
    """Rende i prodotti dell'ordine in una stringa leggibile."""
    if not dati_ordine or not dati_ordine.get("prodotti"):
        return "n/d"
    righe = [
        f"{p.get('quantita')}x {p.get('titolo')} ({p.get('prezzo')})"
        for p in dati_ordine["prodotti"]
    ]
    return "; ".join(righe)


def genera_bozza(
    client: anthropic.Anthropic,
    categoria: str,
    numero_ordine: str | None,
    dati_ordine: dict | None,
    delivery_info: dict,
    decisione: dict,
) -> str:
    """Genera la bozza di risposta (o nota interna) tramite Claude.

    Se la chiamata API fallisce, restituisce un testo di fallback così che il
    pacchetto deterministico resti comunque utilizzabile dall'operatore.
    """
    contesto_utente = (
        f"ESITO DECISO DAL MOTORE: {decisione['esito_proposto']}\n"
        f"MOTIVAZIONE: {decisione['motivazione']}\n"
        f"PROSSIMA AZIONE PREVISTA: {decisione['prossima_azione']}\n"
        f"CATEGORIA RICHIESTA: {categoria}\n"
        f"NUMERO ORDINE: {numero_ordine or 'non fornito'}\n"
        f"PRODOTTI: {_formatta_prodotti(dati_ordine)}\n"
        f"GIORNI DALLA CONSEGNA: {decisione.get('giorni_dalla_consegna')}\n\n"
        "VINCOLI POLICY DA COMUNICARE: "
        f"{json.dumps(decisione, ensure_ascii=False)}\n\n"
        "Scrivi il testo coerente con l'esito, seguendo le istruzioni di sistema."
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=PROMPT_BOZZA,
            messages=[{"role": "user", "content": contesto_utente}],
        )
        return "".join(
            b.text for b in response.content if b.type == "text"
        ).strip()
    except anthropic.APIError as exc:
        return (
            "[BOZZA NON GENERATA] Errore nella chiamata al modello: "
            f"{exc}. L'operatore proceda usando CONTESTO e AZIONE PROPOSTA."
        )


def rigenera_bozza(
    return_case: dict,
    instructions: str,
    *,
    feedback_examples: list[dict] | None = None,
) -> str:
    """Rigenera solo il testo, senza permettere al modello di cambiare l'esito."""
    decision = return_case.get("policy_decision") or {}
    examples = []
    for item in (feedback_examples or [])[:3]:
        examples.append(
            {
                "motivo": item.get("reason_tag"),
                "istruzione": item.get("instructions"),
                "versione_approvata": item.get("revised_draft"),
            }
        )
    prompt = (
        f"ESITO IMMUTABILE: {return_case.get('suggested_resolution')}\n"
        f"POLICY E VINCOLI: {json.dumps(decision, ensure_ascii=False)}\n"
        f"ORDINE: #{return_case.get('shopify_order_number')}\n"
        f"CLIENTE: {return_case.get('customer_name') or 'cliente'}\n"
        f"PRODOTTO: {return_case.get('product_name') or 'prodotto'}\n"
        f"BOZZA ATTUALE: {return_case.get('original_suggested_response') or ''}\n\n"
        f"FEEDBACK OPERATORE: {instructions[:1000]}\n"
        f"ESEMPI APPROVATI DELLA STESSA CATEGORIA: "
        f"{json.dumps(examples, ensure_ascii=False)}\n\n"
        "Riscrivi SOLO la risposta cliente. Mantieni invariata la decisione della "
        "policy e non inventare azioni, importi o tempi."
    )
    client = _crea_client()
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=PROMPT_BOZZA,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


# Sentinella: distingue "dati ordine non forniti dal chiamante" (da recuperare
# qui) da "dati ordine assenti" (None passato esplicitamente).
_NON_FORNITO = object()


def costruisci_pacchetto(
    categoria: str,
    numero_ordine: str | None,
    *,
    dati_ordine=_NON_FORNITO,
    nota_tecnica: str | None = None,
    prove_fornite: bool = False,
    sigillo_integro: bool | None = None,
    confidence: float | None = None,
    requested_resolution: str | None = None,
    escalation_reason: str | None = None,
) -> dict:
    """Aziona il motore (Shopify + tracking + regole) e compone il pacchetto.

    Punto d'ingresso riusabile: riceve categoria e numero_ordine GIÀ noti e
    produce il pacchetto operatore (policy §9). Usato sia da processa_ticket
    (che prima classifica) sia dal livello conversazione (che raccoglie i dati
    a più battute).

    Se il chiamante ha già i dati ordine (es. per validarne l'esistenza), può
    passarli via `dati_ordine` per evitare una seconda chiamata a Shopify.
    """
    delivery_info = dict(DELIVERY_VUOTO)

    if numero_ordine is not None:
        if dati_ordine is _NON_FORNITO:
            try:
                dati_ordine = shopify_client.get_order(numero_ordine)
            except shopify_client.ShopifyError as exc:
                # Errore tecnico (auth/rete): dati non disponibili, ma teniamo
                # traccia del motivo reale per l'operatore.
                dati_ordine = None
                nota_tecnica = f"Errore Shopify: {exc}"
        delivery_info = tracking.get_delivery_info(numero_ordine)
    elif dati_ordine is _NON_FORNITO:
        dati_ordine = None

    # Applicazione delle regole (decisione deterministica).
    decisione = rules.applica_regole(
        categoria, numero_ordine, dati_ordine, delivery_info,
        prove_fornite=prove_fornite,
        sigillo_integro=sigillo_integro,
        confidence=confidence,
        requested_resolution=requested_resolution,
        escalation_reason=escalation_reason,
    )

    # Generazione della bozza di risposta (linguaggio, non decisione).
    client = _crea_client()
    bozza = genera_bozza(
        client, categoria, numero_ordine, dati_ordine, delivery_info, decisione
    )

    # --- Composizione del pacchetto operatore (policy §9) ---
    contesto = {
        "numero_ordine": numero_ordine,
        "prodotti": _formatta_prodotti(dati_ordine),
        "data_consegna": delivery_info.get("delivered_at"),
        "giorni_trascorsi": decisione.get("giorni_dalla_consegna"),
        "categoria": categoria,
        "regola_applicata": decisione.get("motivazione"),
        "ordine": dati_ordine,
        "confidence": confidence,
        "requested_resolution": requested_resolution,
        "escalation_reason": escalation_reason,
    }
    if nota_tecnica:
        contesto["nota_tecnica"] = nota_tecnica

    return {
        "contesto": contesto,
        "policy_evaluation": decisione,
        "azione_proposta": {
            "esito_proposto": decisione.get("esito_proposto"),
            "prossima_azione": decisione.get("prossima_azione"),
        },
        "bozza_risposta": bozza,
    }


def processa_ticket(percorso_file_ticket: str | Path) -> dict:
    """Esegue l'intera pipeline su un ticket e restituisce il pacchetto operatore."""
    ticket_path = Path(percorso_file_ticket)

    # 1) Classificazione (categoria + numero_ordine) dal classifier.
    classificazione = classifier.classifica(ticket_path)

    # 2-4) Motore + packaging (riusabile).
    return costruisci_pacchetto(
        classificazione["categoria"],
        classificazione["numero_ordine"],
        confidence=classificazione.get("confidence"),
        requested_resolution=classificazione.get("requested_resolution"),
        escalation_reason=classificazione.get("escalation_reason"),
    )


def processa_testo(testo_ticket: str) -> dict:
    """Variante di processa_ticket che accetta il testo del ticket direttamente.

    Utile per l'interfaccia web (il cliente digita il messaggio) senza dover
    salvare manualmente un file. Riusa integralmente la pipeline esistente:
    scrive il testo in un file temporaneo e delega a processa_ticket, così la
    logica resta una sola. Non altera l'uso da CLI.
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(testo_ticket)
        return processa_ticket(tmp_path)
    finally:
        os.unlink(tmp_path)


def _stampa_pacchetto(pacchetto: dict) -> None:
    """Stampa il pacchetto operatore in forma leggibile."""
    c = pacchetto["contesto"]
    a = pacchetto["azione_proposta"]

    print("=" * 78)
    print("CONTESTO")
    print("-" * 78)
    print(f"  Numero ordine     : {c['numero_ordine'] or 'non fornito'}")
    print(f"  Prodotti          : {c['prodotti']}")
    print(f"  Data consegna     : {c['data_consegna'] or 'n/d'}")
    print(f"  Giorni trascorsi  : {c['giorni_trascorsi'] if c['giorni_trascorsi'] is not None else 'n/d'}")
    print(f"  Categoria         : {c['categoria']}")
    print(f"  Regola applicata  : {c['regola_applicata']}")
    if c.get("nota_tecnica"):
        print(f"  Nota tecnica      : {c['nota_tecnica']}")

    print("=" * 78)
    print("AZIONE PROPOSTA")
    print("-" * 78)
    print(f"  Esito             : {a['esito_proposto']}")
    print(f"  Prossima azione   : {a['prossima_azione']}")

    print("=" * 78)
    print("BOZZA RISPOSTA")
    print("-" * 78)
    print(pacchetto["bozza_risposta"])
    print("=" * 78)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Uso: python src/agent.py <percorso_file_ticket>", file=sys.stderr)
        return 2

    try:
        pacchetto = processa_ticket(argv[1])
    except FileNotFoundError as exc:
        print(f"Errore: file non trovato -> {exc.filename}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Errore di configurazione: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Errore nel parsing della classificazione: {exc}", file=sys.stderr)
        return 1
    except anthropic.APIError as exc:
        print(f"Errore API Anthropic: {exc}", file=sys.stderr)
        return 1

    _stampa_pacchetto(pacchetto)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.exit(main(sys.argv))
