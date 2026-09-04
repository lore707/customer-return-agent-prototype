"""Crea dati Shopify sintetici per provare il workflow Return Agent.

Per sicurezza lo script non modifica nulla senza ``--execute`` e accetta di
scrivere solo sul dominio passato esplicitamente con ``--allow-store``.

Esempio:
    python scripts/seed_shopify_experiment.py --allow-store mindroute.myshopify.com
    python scripts/seed_shopify_experiment.py --execute \
        --allow-store mindroute.myshopify.com
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
API_VERSION = "2026-07"
TIMEOUT = 20
REQUIRED_SCOPES = {
    "read_customers",
    "write_customers",
    "read_orders",
    "write_orders",
}

CUSTOMER_CREATE = """
mutation CreateExperimentCustomer($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id }
    userErrors { field message }
  }
}
"""

CUSTOMER_FIND = """
query FindExperimentCustomer($query: String!) {
  customers(first: 2, query: $query) { nodes { id } }
}
"""

ORDER_CREATE = """
mutation CreateExperimentOrder(
  $order: OrderCreateOrderInput!,
  $options: OrderCreateOptionsInput
) {
  orderCreate(order: $order, options: $options) {
    order {
      id
      legacyResourceId
      name
      test
      displayFinancialStatus
      displayFulfillmentStatus
      customer { id }
    }
    userErrors { field message }
  }
}
"""

VERIFY_CREATED = """
query VerifyExperimentData($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Customer { id }
    ... on Order { id name test customer { id } }
  }
}
"""


class SeedError(RuntimeError):
    """Errore leggibile durante il caricamento dei dati demo."""


def _scenarios() -> list[dict]:
    """I venti casi coprono le principali diramazioni del prototipo."""
    return [
        {
            "code": "recesso_entro_4g",
            "first_name": "Anna",
            "last_name": "Demo 01",
            "product": "Asciugacapelli Air Pro",
            "sku": "HAIR-AIR-PRO",
            "price": "149.90",
            "delivery_days": 4,
            "message": "Ordine #{order}: l'asciugacapelli funziona, ma ho cambiato idea e vorrei restituirlo.",
            "expected": "procedi_rimborso",
        },
        {
            "code": "recesso_entro_12g",
            "first_name": "Marco",
            "last_name": "Demo 02",
            "product": "Asciugacapelli Air Pro",
            "sku": "HAIR-AIR-PRO",
            "price": "149.90",
            "delivery_days": 12,
            "message": "Vorrei esercitare il diritto di recesso per l'ordine #{order}. Il prodotto non ha difetti.",
            "expected": "procedi_rimborso",
        },
        {
            "code": "recesso_fuori_termini",
            "first_name": "Giulia",
            "last_name": "Demo 03",
            "product": "Asciugacapelli Mini",
            "sku": "HAIR-MINI",
            "price": "89.90",
            "delivery_days": 28,
            "message": "Ordine #{order}: il prodotto funziona ma non mi serve piu e vorrei restituirlo.",
            "expected": "rifiuta_fuori_finestra",
        },
        {
            "code": "rasoio_sigillato",
            "first_name": "Luca",
            "last_name": "Demo 04",
            "product": "Rasoio elettrico Smooth",
            "sku": "SHAVE-SMOOTH",
            "price": "79.90",
            "delivery_days": 5,
            "message": "Ordine #{order}: ho cambiato idea. Rasoio mai aperto, confezione e sigillo sono ancora integri.",
            "expected": "procedi_rimborso",
        },
        {
            "code": "rasoio_aperto",
            "first_name": "Sara",
            "last_name": "Demo 05",
            "product": "Rasoio elettrico Smooth",
            "sku": "SHAVE-SMOOTH",
            "price": "79.90",
            "delivery_days": 5,
            "message": "Ordine #{order}: vorrei fare il reso del rasoio, ma ho gia aperto la confezione e l'ho provato.",
            "expected": "rifiuta_recesso_prodotto_escluso",
        },
        {
            "code": "spazzolino_sigillo_ignoto",
            "first_name": "Paolo",
            "last_name": "Demo 06",
            "product": "Spazzolino Sonic Care",
            "sku": "DENTAL-SONIC",
            "price": "64.90",
            "delivery_days": 6,
            "message": "Vorrei restituire lo spazzolino dell'ordine #{order} perche ho cambiato idea.",
            "expected": "chiedi_stato_sigillo",
        },
        {
            "code": "difettoso_entro_termini",
            "first_name": "Elena",
            "last_name": "Demo 07",
            "product": "Asciugacapelli Air Pro",
            "sku": "HAIR-AIR-PRO",
            "price": "149.90",
            "delivery_days": 3,
            "message": "L'asciugacapelli dell'ordine #{order} non si accende fin dal primo utilizzo.",
            "expected": "chiedi_foto_video",
            "after_evidence": "procedi_swap",
        },
        {
            "code": "difettoso_oltre_termini",
            "first_name": "Davide",
            "last_name": "Demo 08",
            "product": "Aspirapolvere Compact",
            "sku": "HOME-VAC-COMPACT",
            "price": "229.00",
            "delivery_days": 25,
            "message": "Ordine #{order}: l'aspirapolvere ha smesso di funzionare e non parte piu.",
            "expected": "chiedi_foto_video",
            "after_evidence": "procedi_swap",
        },
        {
            "code": "danneggiato_trasporto",
            "first_name": "Chiara",
            "last_name": "Demo 09",
            "product": "Bollitore Glass",
            "sku": "KITCHEN-KETTLE",
            "price": "59.90",
            "delivery_days": 2,
            "message": "Il bollitore dell'ordine #{order} e arrivato rotto dentro il pacco.",
            "expected": "chiedi_foto_video",
            "after_evidence": "procedi_rimborso",
        },
        {
            "code": "articolo_errato",
            "first_name": "Simone",
            "last_name": "Demo 10",
            "product": "Asciugacapelli Air Pro",
            "sku": "HAIR-AIR-PRO",
            "price": "149.90",
            "delivery_days": 4,
            "message": "Ordine #{order}: avevo ordinato il phon Air Pro ma ho ricevuto uno spazzolino.",
            "expected": "escalation_operatore",
        },
        {
            "code": "accessorio_mancante",
            "first_name": "Francesca",
            "last_name": "Demo 11",
            "product": "Aspirapolvere Compact",
            "sku": "HOME-VAC-COMPACT",
            "price": "229.00",
            "delivery_days": 7,
            "message": "Nell'ordine #{order} manca la bocchetta piccola prevista nella confezione.",
            "expected": "escalation_operatore",
        },
        {
            "code": "ordine_non_consegnato",
            "first_name": "Andrea",
            "last_name": "Demo 12",
            "product": "Asciugacapelli Mini",
            "sku": "HAIR-MINI",
            "price": "89.90",
            "delivery_days": None,
            "message": "Vorrei restituire l'ordine #{order}, ma non mi e stato ancora consegnato.",
            "expected": "ordine_non_consegnato",
        },
        {
            "code": "richiesta_sostituzione",
            "first_name": "Valentina",
            "last_name": "Demo 13",
            "product": "Spazzolino Sonic Care",
            "sku": "DENTAL-SONIC",
            "price": "64.90",
            "delivery_days": 18,
            "message": "Lo spazzolino dell'ordine #{order} non si ricarica. Vorrei una sostituzione.",
            "expected": "chiedi_foto_video",
            "after_evidence": "procedi_swap",
        },
        {
            "code": "richiesta_rimborso_difetto",
            "first_name": "Matteo",
            "last_name": "Demo 14",
            "product": "Bollitore Glass",
            "sku": "KITCHEN-KETTLE",
            "price": "59.90",
            "delivery_days": 6,
            "message": "Il bollitore dell'ordine #{order} perde acqua dalla base. Chiedo il rimborso.",
            "expected": "chiedi_foto_video",
            "after_evidence": "procedi_swap",
        },
        {
            "code": "minaccia_chargeback",
            "first_name": "Alessia",
            "last_name": "Demo 15",
            "product": "Asciugacapelli Air Pro",
            "sku": "HAIR-AIR-PRO",
            "price": "149.90",
            "delivery_days": 10,
            "message": "Ordine #{order}: se non mi rimborsate subito apro un chargeback e procedo per vie legali.",
            "expected": "escalation_operatore",
        },
        {
            "code": "richiesta_ambigua",
            "first_name": "Stefano",
            "last_name": "Demo 16",
            "product": "Asciugacapelli Mini",
            "sku": "HAIR-MINI",
            "price": "89.90",
            "delivery_days": 8,
            "message": "Ho un problema con l'ordine #{order}, potete aiutarmi?",
            "expected": "richiesta_chiarimento_o_escalation",
        },
        {
            "code": "difetto_alto_valore",
            "first_name": "Ilaria",
            "last_name": "Demo 17",
            "product": "Styler Premium Pro",
            "sku": "HAIR-STYLER-PREMIUM",
            "price": "599.00",
            "delivery_days": 9,
            "message": "Lo styler premium dell'ordine #{order} si spegne dopo pochi secondi.",
            "expected": "chiedi_foto_video",
        },
        {
            "code": "secondo_articolo_errato",
            "first_name": "Roberto",
            "last_name": "Demo 18",
            "product": "Rasoio elettrico Smooth",
            "sku": "SHAVE-SMOOTH",
            "price": "79.90",
            "delivery_days": 3,
            "message": "Per l'ordine #{order} mi avete inviato il modello sbagliato, diverso da quello acquistato.",
            "expected": "escalation_operatore",
        },
        {
            "code": "secondo_danno_trasporto",
            "first_name": "Federica",
            "last_name": "Demo 19",
            "product": "Aspirapolvere Compact",
            "sku": "HOME-VAC-COMPACT",
            "price": "229.00",
            "delivery_days": 1,
            "message": "Ordine #{order}: il corpo dell'aspirapolvere e arrivato crepato e il pacco era schiacciato.",
            "expected": "chiedi_foto_video",
            "after_evidence": "procedi_rimborso",
        },
        {
            "code": "secondo_recesso_standard",
            "first_name": "Giorgio",
            "last_name": "Demo 20",
            "product": "Bollitore Glass",
            "sku": "KITCHEN-KETTLE",
            "price": "59.90",
            "delivery_days": 13,
            "message": "Ordine #{order}: il bollitore funziona bene, ma ho cambiato idea e desidero restituirlo.",
            "expected": "procedi_rimborso",
        },
    ]


def _normalise_store(value: str) -> str:
    store = value.strip().lower().replace("https://", "").replace("http://", "")
    store = store.rstrip("/")
    if not store.endswith(".myshopify.com"):
        store = f"{store}.myshopify.com"
    return store


def _graphql(store: str, token: str, query: str, variables: dict) -> dict:
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    try:
        response = requests.post(
            url,
            headers={
                "X-Shopify-Access-Token": token,
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SeedError(f"Errore di rete verso Shopify: {exc}") from exc

    if response.status_code != 200:
        raise SeedError(
            f"Shopify ha risposto HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise SeedError("Shopify ha restituito una risposta non JSON.") from exc
    if payload.get("errors"):
        messages = "; ".join(error.get("message", "Errore GraphQL") for error in payload["errors"])
        raise SeedError(messages)
    return payload["data"]


def _access_scopes(store: str, token: str) -> set[str]:
    url = f"https://{store}/admin/oauth/access_scopes.json"
    response = requests.get(
        url,
        headers={"X-Shopify-Access-Token": token},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise SeedError(f"Impossibile verificare i permessi Shopify (HTTP {response.status_code}).")
    return {item["handle"] for item in response.json().get("access_scopes", [])}


def _shop_identity(store: str, token: str) -> dict:
    data = _graphql(
        store,
        token,
        "query SeedShopIdentity { shop { name myshopifyDomain } }",
        {},
    )
    return data["shop"]


def _payload_error(result: dict, operation: str) -> None:
    errors = result.get("userErrors") or []
    if errors:
        details = "; ".join(
            f"{'.'.join(map(str, error.get('field') or []))}: {error.get('message')}"
            for error in errors
        )
        raise SeedError(f"{operation} rifiutata da Shopify: {details}")


def _create_customer(store: str, token: str, scenario: dict, batch: str, index: int) -> dict:
    email = f"return-agent-{batch}-{index:02d}@example.com"
    existing = _graphql(
        store,
        token,
        CUSTOMER_FIND,
        {"query": f"email:{email}"},
    )["customers"]["nodes"]
    if len(existing) > 1:
        raise SeedError(f"Trovati piu clienti con la stessa e-mail sintetica: {email}")
    if existing:
        return {
            "id": existing[0]["id"],
            "displayName": f"{scenario['first_name']} {scenario['last_name']}",
            "email": email,
        }

    data = _graphql(
        store,
        token,
        CUSTOMER_CREATE,
        {
            "input": {
                "email": email,
                "firstName": scenario["first_name"],
                "lastName": scenario["last_name"],
                "locale": "it",
                "note": "Cliente sintetico per il test Customer Return Agent. Non contattare.",
                "tags": ["return-agent-experiment", batch, scenario["code"]],
            }
        },
    )
    result = data["customerCreate"]
    _payload_error(result, "Creazione cliente")
    return {
        "id": result["customer"]["id"],
        "displayName": f"{scenario['first_name']} {scenario['last_name']}",
        "email": email,
    }


def _create_order(
    store: str,
    token: str,
    customer_id: str,
    scenario: dict,
    batch: str,
    today: date,
) -> dict:
    delivered = scenario["delivery_days"] is not None
    purchase_days = (scenario["delivery_days"] or 0) + 5
    processed_at = datetime.combine(
        today - timedelta(days=purchase_days), time(12, 0), timezone.utc
    ).isoformat()
    order_input = {
        "currency": "EUR",
        "customer": {"toAssociate": {"id": customer_id}},
        "financialStatus": "PENDING",
        "lineItems": [
            {
                "title": scenario["product"],
                "sku": scenario["sku"],
                "vendor": "MindRoute Demo",
                "quantity": 1,
                "requiresShipping": True,
                "taxable": False,
                "priceSet": {
                    "shopMoney": {
                        "amount": scenario["price"],
                        "currencyCode": "EUR",
                    }
                },
            }
        ],
        "note": (
            "ORDINE SINTETICO - Customer Return Agent. "
            f"Scenario: {scenario['code']}. Esito atteso iniziale: {scenario['expected']}."
        ),
        "processedAt": processed_at,
        "sourceName": "return-agent-experiment",
        "tags": ["return-agent-experiment", batch, scenario["code"]],
        "test": True,
    }
    if delivered:
        order_input["fulfillmentStatus"] = "FULFILLED"

    data = _graphql(
        store,
        token,
        ORDER_CREATE,
        {
            "order": order_input,
            "options": {
                "sendReceipt": False,
                "sendFulfillmentReceipt": False,
            },
        },
    )
    result = data["orderCreate"]
    _payload_error(result, "Creazione ordine")
    return result["order"]


def _save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_manifest(path: Path, batch: str, store: str) -> dict:
    if not path.exists():
        return {
            "batch": batch,
            "shop": store,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "safety": {
                "synthetic_data": True,
                "test_orders": True,
                "emails_sent": False,
                "transactions_created": False,
            },
            "items": [],
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("batch") != batch or data.get("shop") != store:
        raise SeedError(f"Il manifest esistente non corrisponde al batch/store: {path}")
    return data


def _merge_delivery_data(manifest: dict, today: date) -> None:
    path = ROOT / "deliveries.json"
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for item in manifest["items"]:
        if not item.get("order_number"):
            continue
        days = item.get("delivery_days")
        delivered_at = (today - timedelta(days=days)).isoformat() if days is not None else None
        current[str(item["order_number"])] = {
            "tracking": f"EXP{item['order_number']}IT" if delivered_at else None,
            "delivered_at": delivered_at,
        }
    _save_json(path, current)


def _write_guide(path: Path, manifest: dict) -> None:
    lines = [
        f"# Test Shopify - {manifest['batch']}",
        "",
        f"Negozio: `{manifest['shop']}`",
        "",
        "Copia un messaggio nella sezione **Nuova pratica** della dashboard. Gli ordini sono test, senza pagamento e senza e-mail.",
        "",
    ]
    for item in manifest["items"]:
        lines.extend(
            [
                f"## {item['index']:02d}. {item['scenario']} - ordine {item['order_name']}",
                "",
                f"Cliente: {item['customer_name']}  ",
                f"Prodotto: {item['product']} (`{item['sku']}`)  ",
                f"Esito iniziale atteso: `{item['expected']}`",
                "",
                f"> {item['customer_message']}",
                "",
            ]
        )
        if item.get("after_evidence"):
            lines.extend(
                [
                    f"Dopo **Conferma prove ricevute**: `{item['after_evidence']}`.",
                    "",
                ]
            )
    lines.extend(
        [
            "## Due test senza ordine Shopify dedicato",
            "",
            "> Vorrei restituire il prodotto perche ho cambiato idea.",
            "",
            "Atteso: richiesta del numero d'ordine.",
            "",
            "> L'ordine #000000 contiene un prodotto difettoso che non si accende.",
            "",
            "Atteso: ordine non trovato e richiesta di verificare il numero.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _verify(manifest: dict, store: str, token: str) -> None:
    ids = []
    for item in manifest["items"]:
        ids.extend([item["customer_id"], item["order_id"]])
    nodes = _graphql(store, token, VERIFY_CREATED, {"ids": ids})["nodes"]
    found = [node for node in nodes if node]
    test_orders = [node for node in found if node.get("name") and node.get("test") is True]
    if len(found) != len(ids) or len(test_orders) != len(manifest["items"]):
        raise SeedError(
            f"Verifica incompleta: {len(found)}/{len(ids)} oggetti, "
            f"{len(test_orders)}/{len(manifest['items'])} ordini test."
        )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="esegue davvero le creazioni")
    parser.add_argument("--allow-store", required=True, help="dominio Shopify autorizzato")
    parser.add_argument(
        "--batch",
        default=f"return-agent-{date.today().strftime('%Y%m%d')}",
        help="identificatore del batch; il valore predefinito e giornaliero",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,40}", args.batch):
        raise SeedError("Il batch deve contenere solo lettere minuscole, numeri e trattini.")

    load_dotenv(ROOT / ".env")
    configured_store = os.getenv("SHOPIFY_STORE")
    token = os.getenv("SHOPIFY_TOKEN")
    if not configured_store or not token:
        raise SeedError("SHOPIFY_STORE o SHOPIFY_TOKEN mancanti nel file .env.")

    store = _normalise_store(configured_store)
    allowed_store = _normalise_store(args.allow_store)
    if store != allowed_store:
        raise SeedError(f"Store configurato '{store}' diverso da quello autorizzato '{allowed_store}'.")

    shop = _shop_identity(store, token)
    if _normalise_store(shop["myshopifyDomain"]) != store:
        raise SeedError("L'identita restituita da Shopify non coincide con lo store configurato.")

    scopes = _access_scopes(store, token)
    missing = REQUIRED_SCOPES - scopes
    if missing:
        raise SeedError(f"Permessi Shopify mancanti: {', '.join(sorted(missing))}")

    scenarios = _scenarios()
    print(f"Negozio verificato: {shop['name']} ({store})")
    print(f"Piano: {len(scenarios)} clienti sintetici + {len(scenarios)} ordini test")
    print("Sicurezza: nessuna e-mail, transazione, rimborso o spedizione reale")
    if not args.execute:
        print("DRY RUN: nessun dato creato. Aggiungi --execute per procedere.")
        return 0

    manifest_path = ROOT / "data" / f"shopify_experiment_{args.batch}.json"
    guide_path = ROOT / "data" / f"shopify_experiment_{args.batch}.md"
    manifest = _load_manifest(manifest_path, args.batch, store)
    existing = {item["index"]: item for item in manifest["items"]}
    today = date.today()

    for index, scenario in enumerate(scenarios, start=1):
        item = existing.get(index)
        if item and item.get("order_id"):
            print(f"[{index:02d}/20] gia presente: {item['order_name']} ({scenario['code']})")
            continue

        if item and item.get("customer_id"):
            customer = {
                "id": item["customer_id"],
                "displayName": item["customer_name"],
                "email": item["customer_email"],
            }
        else:
            customer = _create_customer(store, token, scenario, args.batch, index)
            item = {
                "index": index,
                "scenario": scenario["code"],
                "customer_id": customer["id"],
                "customer_name": customer["displayName"],
                "customer_email": customer["email"],
                "order_id": None,
                "order_number": None,
                "order_name": None,
                "product": scenario["product"],
                "sku": scenario["sku"],
                "price_eur": scenario["price"],
                "delivery_days": scenario["delivery_days"],
                "expected": scenario["expected"],
                "after_evidence": scenario.get("after_evidence"),
                "customer_message": None,
            }
            manifest["items"].append(item)
            existing[index] = item
            _save_json(manifest_path, manifest)

        order = _create_order(store, token, customer["id"], scenario, args.batch, today)
        order_number = str(order["name"]).lstrip("#")
        item.update(
            {
                "order_id": order["id"],
                "order_legacy_id": order["legacyResourceId"],
                "order_number": order_number,
                "order_name": order["name"],
                "is_test_order": order["test"],
                "financial_status": order["displayFinancialStatus"],
                "fulfillment_status": order["displayFulfillmentStatus"],
                "customer_message": scenario["message"].format(order=order_number),
            }
        )
        _save_json(manifest_path, manifest)
        print(f"[{index:02d}/20] creato {order['name']} - {scenario['code']}")

    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    _verify(manifest, store, token)
    _merge_delivery_data(manifest, today)
    _write_guide(guide_path, manifest)
    _save_json(manifest_path, manifest)
    print("Verifica completata: 20 clienti e 20 ordini test trovati su Shopify.")
    print(f"Guida messaggi: {guide_path}")
    print(f"Manifest tecnico: {manifest_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedError as exc:
        print(f"ERRORE: {exc}", file=sys.stderr)
        sys.exit(1)
