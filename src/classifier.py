"""Modulo di classificazione ticket customer care (MVP).

Legge un ticket e le policy aziendali, chiama l'API Anthropic e restituisce
un JSON con categoria, numero d'ordine e una nota per l'operatore.

Uso:
    python src/classifier.py tickets/ticket05.txt
"""

import json
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

# Il modello richiesto per l'MVP.
MODEL = "claude-sonnet-4-6"

# Categorie ammesse (devono restare allineate con policies.md).
CATEGORIE = ["recesso", "doa", "arrivato_rotto", "articolo_errato", "altro"]

# La cartella radice del progetto (una su rispetto a src/).
ROOT = Path(__file__).resolve().parent.parent
POLICIES_PATH = ROOT / "policies.md"


PROMPT_SISTEMA = """Sei un assistente di customer care per e-commerce. Il tuo compito \
è classificare il ticket di un cliente basandoti ESCLUSIVAMENTE sulle regole \
aziendali qui sotto.

<policy>
{policy}
</policy>

Devi restituire SOLO un oggetto JSON valido, senza testo prima o dopo, senza \
markdown e senza blocchi di codice. Lo schema è:

{{
  "categoria": "<una tra: recesso | doa | arrivato_rotto | articolo_errato | altro>",
  "numero_ordine": "<la stringa del numero ordine citata dal cliente, oppure null se assente>",
  "requested_resolution": "<refund | swap | null; valorizza solo se il cliente ha espresso una preferenza>",
  "escalation_reason": "<legal_threat | chargeback | consumer_complaint | policy_exception | decision_dispute | potential_fraud | ambiguous_evidence | multiple_orders | product_not_identified | null>",
  "confidence": "<numero tra 0 e 1>",
  "note": "<breve nota operativa in italiano>"
}}

Linee guida per la categoria:
- "recesso": il cliente ha cambiato idea / ripensamento entro i termini, non ci sono difetti.
- "doa": prodotto difettoso o malfunzionante (non si accende, guasto).
- "arrivato_rotto": danno da trasporto, prodotto arrivato rotto/danneggiato all'apertura.
- "articolo_errato": ricevuto un articolo diverso da quello ordinato (nostro errore di spedizione).
- "altro": tutto ciò che non rientra sopra, o casi da escalation (minacce legali, chargeback, reclami).

Regole per numero_ordine:
- Estrai il numero SOLO se il cliente lo cita esplicitamente nel testo.
- Se non è presente, imposta numero_ordine a null e nella nota segnala che va richiesto al cliente."""


def leggi_file(path: Path) -> str:
    """Legge un file di testo, sollevando FileNotFoundError se assente."""
    return path.read_text(encoding="utf-8")


def estrai_json(testo: str) -> dict:
    """Prova a interpretare la risposta del modello come JSON.

    Gestisce sia una risposta JSON pura sia il caso in cui il modello
    inserisca il JSON dentro altro testo, cercando il primo blocco {...}.
    Solleva ValueError se non è possibile ottenere un JSON valido.
    """
    testo = testo.strip()
    try:
        return json.loads(testo)
    except json.JSONDecodeError:
        pass

    inizio = testo.find("{")
    fine = testo.rfind("}")
    if inizio != -1 and fine != -1 and fine > inizio:
        frammento = testo[inizio : fine + 1]
        try:
            return json.loads(frammento)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Risposta non JSON dal modello: {testo!r}") from exc

    raise ValueError(f"Risposta non JSON dal modello: {testo!r}")


def normalizza(dati: dict) -> dict:
    """Valida e normalizza i campi restituiti dal modello."""
    categoria = dati.get("categoria")
    if categoria not in CATEGORIE:
        categoria = "altro"

    numero_ordine = dati.get("numero_ordine")
    if numero_ordine is not None:
        numero_ordine = str(numero_ordine).strip() or None

    note = dati.get("note") or ""

    requested_resolution = dati.get("requested_resolution")
    if requested_resolution not in {"refund", "swap"}:
        requested_resolution = None

    escalation_reason = dati.get("escalation_reason")
    allowed_escalations = {
        "legal_threat", "chargeback", "consumer_complaint", "policy_exception",
        "decision_dispute", "potential_fraud", "ambiguous_evidence",
        "multiple_orders", "product_not_identified",
    }
    if escalation_reason not in allowed_escalations:
        escalation_reason = None

    try:
        confidence = float(dati.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return {
        "categoria": categoria,
        "numero_ordine": numero_ordine,
        "requested_resolution": requested_resolution,
        "escalation_reason": escalation_reason,
        "confidence": confidence,
        "note": str(note).strip(),
    }


def classifica(ticket_path: Path) -> dict:
    """Classifica un singolo ticket e restituisce il dizionario risultato."""
    testo_ticket = leggi_file(ticket_path)
    testo_policy = leggi_file(POLICIES_PATH)

    load_dotenv(ROOT / ".env")
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY mancante. Inseriscila nel file .env alla riga "
            "'ANTHROPIC_API_KEY='."
        )

    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=PROMPT_SISTEMA.format(policy=testo_policy),
        messages=[
            {
                "role": "user",
                "content": (
                    "Classifica il seguente ticket del cliente.\n\n"
                    f"<ticket>\n{testo_ticket}\n</ticket>"
                ),
            }
        ],
    )

    testo_risposta = "".join(
        blocco.text for blocco in response.content if blocco.type == "text"
    )
    dati = estrai_json(testo_risposta)
    return normalizza(dati)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Uso: python src/classifier.py <percorso_file_ticket>", file=sys.stderr)
        return 2

    ticket_path = Path(argv[1])

    try:
        risultato = classifica(ticket_path)
    except FileNotFoundError as exc:
        print(f"Errore: file non trovato -> {exc.filename}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Errore di configurazione: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Errore nel parsing della risposta: {exc}", file=sys.stderr)
        return 1
    except anthropic.APIError as exc:
        print(f"Errore API Anthropic: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(risultato, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
