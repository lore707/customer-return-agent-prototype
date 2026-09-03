"""Lettore delle informazioni di consegna (MVP).

Simula l'API del corriere leggendo da deliveries.json. In produzione questa
funzione verrà sostituita da una vera chiamata all'API del corriere, ma
l'interfaccia (get_delivery_info -> {tracking, delivered_at}) resta identica.

Uso:
    python src/tracking.py 1001
"""

import json
import sys
from pathlib import Path

# Cartella radice del progetto (una su rispetto a src/).
ROOT = Path(__file__).resolve().parent.parent
DELIVERIES_PATH = ROOT / "deliveries.json"


def normalizza_numero(order_number: str) -> str:
    """Normalizza il numero ordine rimuovendo un eventuale '#' iniziale.

    In deliveries.json le chiavi sono numeri puri (es. "1001"), quindi qui
    riportiamo l'input alla forma senza '#'. Accetta sia "1001" sia "#1001".
    """
    return str(order_number).strip().lstrip("#").strip()


def get_delivery_info(order_number: str) -> dict:
    """Restituisce {tracking, delivered_at} per l'ordine indicato.

    Se l'ordine non ha una consegna registrata (o non è presente nel file),
    restituisce {tracking: None, delivered_at: None}.
    """
    numero = normalizza_numero(order_number)

    with DELIVERIES_PATH.open(encoding="utf-8") as f:
        consegne = json.load(f)

    dati = consegne.get(numero)
    if not dati:
        return {"tracking": None, "delivered_at": None}

    return {
        "tracking": dati.get("tracking"),
        "delivered_at": dati.get("delivered_at"),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Uso: python src/tracking.py <numero_ordine>", file=sys.stderr)
        return 2

    try:
        info = get_delivery_info(argv[1])
    except FileNotFoundError:
        print(f"Errore: file non trovato -> {DELIVERIES_PATH}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Errore: deliveries.json non è un JSON valido: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
