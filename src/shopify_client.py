"""Client Shopify Admin API (MVP).

Dato un numero d'ordine (es. "1001" o "#1001"), interroga l'Admin API REST di
Shopify e restituisce un dizionario pulito con i soli campi utili all'agente.

Uso:
    python src/shopify_client.py 1001
"""

import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

# Versione stabile usata anche dallo script di seed Shopify.
API_VERSION = "2026-07"

# Timeout (secondi) per le chiamate HTTP.
TIMEOUT = 15

# Cartella radice del progetto (una su rispetto a src/).
ROOT = Path(__file__).resolve().parent.parent


class ShopifyError(RuntimeError):
    """Errore non recuperabile lato Shopify (auth, rete, risposta invalida)."""


def normalizza_name(order_number: str) -> str:
    """Normalizza il numero ordine nel formato Shopify '#1001'.

    Accetta sia "1001" sia "#1001" (con eventuali spazi).
    """
    numero = str(order_number).strip().lstrip("#").strip()
    return f"#{numero}"


def _base_url(store: str) -> str:
    """Costruisce l'URL base dell'Admin API a partire da SHOPIFY_STORE.

    Accetta sia il solo subdominio ("mio-store") sia il dominio completo
    ("mio-store.myshopify.com").
    """
    store = store.strip().rstrip("/")
    # Rimuove un eventuale schema (https://) se l'utente lo ha incluso.
    if "://" in store:
        store = store.split("://", 1)[1]
    if not store.endswith(".myshopify.com"):
        store = f"{store}.myshopify.com"
    return f"https://{store}/admin/api/{API_VERSION}"


def _pulisci_ordine(order: dict) -> dict:
    """Estrae dall'ordine Shopify solo i campi che servono all'agente."""
    prodotti = [
        {
            "line_item_id": item.get("id"),
            "titolo": item.get("title"),
            "sku": item.get("sku"),
            "variant_id": item.get("variant_id"),
            "quantita": item.get("quantity"),
            "prezzo": item.get("price"),
        }
        for item in order.get("line_items", [])
    ]

    customer = order.get("customer") or {}
    customer_name = " ".join(
        part for part in (customer.get("first_name"), customer.get("last_name")) if part
    ).strip()

    return {
        "shopify_order_id": order.get("id"),
        "numero_ordine": order.get("name"),
        # fulfillment_status è null quando l'ordine non è ancora evaso.
        "stato_evasione": order.get("fulfillment_status") or "inevaso",
        "prodotti": prodotti,
        "totale_ordine": order.get("total_price"),
        "email_cliente": order.get("email") or order.get("contact_email"),
        "customer_id": customer.get("id"),
        "customer_name": customer_name or None,
        "data_acquisto": order.get("created_at"),
    }


def _integra_cliente_demo(ordine: dict) -> dict:
    """Completa nome/e-mail solo per gli ordini sintetici creati dal progetto.

    Shopify Basic restituisce correttamente ordine, prodotti e customer ID, ma
    oscura i campi PII per le app custom. Il manifest locale contiene soltanto
    identita demo e permette alla dashboard di restare leggibile senza aggirare
    la protezione su clienti reali.
    """
    if ordine.get("customer_name") and ordine.get("email_cliente"):
        return ordine

    numero = str(ordine.get("numero_ordine") or "").lstrip("#")
    if not numero:
        return ordine

    data_dir = ROOT / "data"
    for manifest_path in sorted(data_dir.glob("shopify_experiment_*.json"), reverse=True):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in manifest.get("items", []):
            if str(item.get("order_number") or "").lstrip("#") != numero:
                continue
            ordine["customer_name"] = ordine.get("customer_name") or item.get(
                "customer_name"
            )
            ordine["email_cliente"] = ordine.get("email_cliente") or item.get(
                "customer_email"
            )
            return ordine
    return ordine


def get_order(order_number: str) -> dict | None:
    """Restituisce i dati puliti dell'ordine, oppure None se non trovato.

    Solleva ShopifyError in caso di errore di autenticazione o di rete.
    """
    load_dotenv(ROOT / ".env")
    store = os.getenv("SHOPIFY_STORE")
    token = os.getenv("SHOPIFY_TOKEN")
    if not store or not token:
        raise ShopifyError(
            "SHOPIFY_STORE e/o SHOPIFY_TOKEN mancanti nel file .env."
        )

    name = normalizza_name(order_number)
    url = f"{_base_url(store)}/orders.json"
    params = {"name": name, "status": "any"}
    headers = {
        "X-Shopify-Access-Token": token,
        "Accept": "application/json",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ShopifyError(f"Errore di rete verso Shopify: {exc}") from exc

    if resp.status_code == 401:
        raise ShopifyError(
            "Autenticazione fallita (401): controlla SHOPIFY_TOKEN e i permessi dell'app."
        )
    if resp.status_code != 200:
        raise ShopifyError(
            f"Risposta inattesa da Shopify (HTTP {resp.status_code}): {resp.text[:200]}"
        )

    try:
        ordini = resp.json().get("orders", [])
    except ValueError as exc:
        raise ShopifyError("Risposta non JSON da Shopify.") from exc

    # Il filtro 'name' di Shopify può essere permissivo: confermiamo la
    # corrispondenza esatta sul campo name normalizzato.
    for order in ordini:
        if order.get("name") == name:
            return _integra_cliente_demo(_pulisci_ordine(order))

    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Uso: python src/shopify_client.py <numero_ordine>", file=sys.stderr)
        return 2

    order_number = argv[1]

    try:
        ordine = get_order(order_number)
    except ShopifyError as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1

    if ordine is None:
        print(
            f"Ordine {normalizza_name(order_number)} non trovato su Shopify.",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(ordine, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
