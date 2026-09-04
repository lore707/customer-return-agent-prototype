"""Motore di regole (MVP) — il cuore decisionale dell'agente.

Funzione PURA: riceve i dati grezzi prodotti dagli altri moduli
(classifier, shopify_client, tracking) e restituisce una decisione secondo
le policy di policies.md. Non effettua chiamate API né legge file, così è
testabile in isolamento e facile da aggiornare quando cambiano le policy.

Uso (test isolato con dati finti):
    python src/rules.py
"""

import json
import sys
from datetime import date

import policy_config

POLICY = policy_config.load_policy()

# Valori applicabili letti dalla policy strutturata, non dai prompt.
FINESTRA_RECESSO_GIORNI = POLICY["withdrawal"]["window_days"]

# Parole chiave (nei titoli prodotto) che identificano articoli esclusi dal
# recesso se la scatola è aperta (policy §3). Lista volutamente estendibile.
PAROLE_PRODOTTI_ESCLUSI = POLICY["withdrawal"]["excluded_product_keywords"]


def _prodotto_soggetto_esclusione(dati_ordine: dict | None) -> bool | None:
    """True se l'ordine contiene un prodotto escluso dal recesso.

    Restituisce None se i dati ordine non sono disponibili (non calcolabile).
    """
    if not dati_ordine:
        return None
    titoli = " ".join(
        str(p.get("titolo") or "").lower() for p in dati_ordine.get("prodotti", [])
    )
    return any(parola in titoli for parola in PAROLE_PRODOTTI_ESCLUSI)


def _prodotto_escluso_recesso(
    dati_ordine: dict | None, sigillo_integro: bool | None
) -> bool | None:
    """Valuta l'esclusione solo quando è noto lo stato del sigillo."""
    soggetto = _prodotto_soggetto_esclusione(dati_ordine)
    if soggetto is None:
        return None
    if not soggetto:
        return False
    if sigillo_integro is None:
        return None
    return not sigillo_integro


def _giorni_dalla_consegna(delivery_info: dict, oggi: date) -> int | None:
    """Giorni trascorsi dalla consegna, oppure None se data non disponibile."""
    delivered_at = (delivery_info or {}).get("delivered_at")
    if not delivered_at:
        return None
    try:
        consegna = date.fromisoformat(delivered_at)
    except (ValueError, TypeError):
        return None
    giorni = (oggi - consegna).days
    return giorni if giorni >= 0 else None


def _ordine_non_consegnato(dati_ordine: dict | None, delivery_info: dict) -> bool:
    """True se l'ordine risulta non ancora consegnato.

    Criterio: nessuna data di consegna dal tracking E stato di evasione 'inevaso'.
    """
    delivered_at = (delivery_info or {}).get("delivered_at")
    stato = (dati_ordine or {}).get("stato_evasione")
    return delivered_at is None and stato == "inevaso"


def _decisione(
    esito: str,
    motivazione: str,
    prossima_azione: str,
    *,
    rule_id: str = "policy_fallback",
    sections: tuple[str, ...] = (),
    **details,
) -> dict:
    """Piccolo helper per costruire la parte 'decisione' del risultato."""
    decision = {
        "esito_proposto": esito,
        "motivazione": motivazione,
        "prossima_azione": prossima_azione,
        "rule_id": rule_id,
        "policy_sections": list(sections),
    }
    decision.update(details)
    return decision


def applica_regole(
    categoria: str,
    numero_ordine: str | None,
    dati_ordine: dict | None,
    delivery_info: dict,
    *,
    oggi: date | None = None,
    prove_fornite: bool = False,
    sigillo_integro: bool | None = None,
    confidence: float | None = None,
    requested_resolution: str | None = None,
    escalation_reason: str | None = None,
) -> dict:
    """Applica le policy e restituisce la decisione per l'operatore.

    Parametri posizionali (come da specifica):
        categoria       stringa dal classifier
        numero_ordine   stringa oppure None
        dati_ordine     dict da shopify_client.get_order, oppure None
        delivery_info   dict da tracking.get_delivery_info

    Parametri keyword opzionali:
        oggi            data di riferimento (default: data odierna del sistema);
                        esplicitabile per test deterministici
        prove_fornite   True se foto/video richiesti per DOA/arrivato_rotto
                        risultano già ricevuti (nell'MVP si assume False)
        sigillo_integro stato del sigillo dichiarato dal cliente, se noto
        confidence      affidabilità della classificazione AI; sotto 0.70
                        il caso viene escalato
        requested_resolution preferenza esplicita del cliente: refund/swap
        escalation_reason segnale strutturato che richiede revisione umana

    Ritorna un dict con: giorni_dalla_consegna, entro_finestra_recesso,
    prodotto_escluso_recesso, esito_proposto, motivazione, prossima_azione.
    """
    oggi = oggi or date.today()

    # --- Campi informativi (calcolati sempre quando possibile) ---
    giorni = _giorni_dalla_consegna(delivery_info, oggi)
    entro_finestra = giorni <= FINESTRA_RECESSO_GIORNI if giorni is not None else None
    prodotto_soggetto_esclusione = _prodotto_soggetto_esclusione(dati_ordine)
    prodotto_escluso = _prodotto_escluso_recesso(dati_ordine, sigillo_integro)

    risultato = {
        "giorni_dalla_consegna": giorni,
        "entro_finestra_recesso": entro_finestra,
        "prodotto_escluso_recesso": prodotto_escluso,
        "prodotto_soggetto_esclusione": prodotto_soggetto_esclusione,
        "policy_version": POLICY["version"],
        "warranty_max_days": POLICY["defective_product"].get("warranty_max_days"),
        "within_warranty": (
            giorni <= POLICY["defective_product"]["warranty_max_days"]
            if giorni is not None
            and POLICY["defective_product"].get("warranty_max_days")
            else None
        ),
        "review_level": (
            "unknown"
            if confidence is None
            else "escalate"
            if confidence < POLICY["escalation"]["low_confidence_below"]
            else "attention"
            if confidence < POLICY["escalation"]["attention_confidence_below"]
            else "standard"
        ),
    }

    # --- Albero decisionale (ordine da policies.md) ---

    if numero_ordine is None:
        risultato.update(
            _decisione(
                "chiedi_numero_ordine",
                "Il numero d'ordine è indispensabile per verificare consegna e prodotti (policy §9).",
                "Chiedere al cliente il numero d'ordine prima di procedere.",
                rule_id="order_number_required",
                sections=("§9",),
                missing_information=["order_number"],
            )
        )
        return risultato

    if dati_ordine is None:
        risultato.update(
            _decisione(
                "escalation_operatore",
                f"Ordine '{numero_ordine}' non trovato: nessuna decisione automatica è sicura.",
                "Verificare numero, store e identità del cliente.",
                rule_id="order_not_found",
                sections=("§8", "§9"),
                escalation_reason="order_not_found",
            )
        )
        return risultato

    if escalation_reason:
        risultato.update(
            _decisione(
                "escalation_operatore",
                f"Il ticket contiene un segnale di escalation: {escalation_reason} (policy §8).",
                "Passare la pratica a un operatore senza promettere una risoluzione.",
                rule_id="explicit_escalation_signal",
                sections=("§8",),
                escalation_reason=escalation_reason,
            )
        )
        return risultato

    if confidence is not None and confidence < POLICY["escalation"]["low_confidence_below"]:
        risultato.update(
            _decisione(
                "escalation_operatore",
                f"Classificazione AI poco affidabile ({confidence:.0%}): serve una verifica umana.",
                "Verificare categoria, prodotto e richiesta del cliente.",
                rule_id="low_confidence_escalation",
                sections=("§8",),
                escalation_reason="low_confidence",
            )
        )
        return risultato

    threshold = POLICY["escalation"].get("high_value_threshold_eur")
    try:
        order_value = float((dati_ordine or {}).get("totale_ordine"))
    except (TypeError, ValueError):
        order_value = None
    if threshold is not None and order_value is not None and order_value > threshold:
        risultato.update(
            _decisione(
                "escalation_operatore",
                f"Valore ordine €{order_value:.2f} superiore alla soglia configurata di €{threshold:.2f}.",
                "Richiedere approvazione manuale per ordine di valore elevato.",
                rule_id="high_value_order_escalation",
                sections=("§8",),
                escalation_reason="high_value_order",
            )
        )
        return risultato

    if len((dati_ordine or {}).get("prodotti") or []) > 1:
        risultato.update(
            _decisione(
                "escalation_operatore",
                "L'ordine contiene più prodotti e il line item interessato non è identificato (policy §7).",
                "Identificare il singolo articolo; per uno swap creare poi un ordine separato con il solo sostitutivo.",
                rule_id="partial_return_line_item_required",
                sections=("§7",),
                missing_information=["line_item"],
                escalation_reason="multiple_products",
            )
        )
        return risultato

    if categoria == "altro":
        risultato.update(
            _decisione(
                "escalation_operatore",
                "Caso fuori dalle regole standard o con segnali legali, frode, eccezione o contestazione (policy §8).",
                "Passare il ticket a un operatore umano.",
                rule_id="non_standard_case_escalation",
                sections=("§8",),
                escalation_reason="non_standard_case",
            )
        )
        return risultato

    if categoria == "articolo_errato":
        risultato.update(
            _decisione(
                "escalation_operatore",
                "L'articolo errato è a carico azienda, ma la risoluzione definitiva è ancora da confermare (policy §2).",
                "Verificare picking e prodotto ricevuto; decidere manualmente reso e invio corretto.",
                rule_id="wrong_item_policy_unresolved",
                sections=("§2", "§8"),
                shipping_payer="company",
                escalation_reason="unresolved_policy",
                unresolved_policy="wrong_item_resolution",
            )
        )
        return risultato

    if _ordine_non_consegnato(dati_ordine, delivery_info):
        risultato.update(
            _decisione(
                "ordine_non_consegnato",
                "L'ordine non è ancora consegnato: la finestra di recesso parte dal tracking (policy §1).",
                "Verificare la spedizione e informare il cliente.",
                rule_id="delivery_required_before_withdrawal",
                sections=("§1",),
                missing_information=["delivery_date"],
            )
        )
        return risultato

    if categoria in ("doa", "arrivato_rotto") and not prove_fornite:
        risultato.update(
            _decisione(
                "chiedi_foto_video",
                "Per difetto o danno da trasporto servono foto e video; senza prove la pratica non procede (policy §5).",
                "Richiedere foto, video e seriale. Chiudere dopo 15 giorni senza risposta.",
                rule_id="evidence_required",
                sections=("§5",),
                missing_information=["photo", "video"],
                timeout_days=POLICY["evidence"]["no_response_close_days"],
                evidence_status="required",
            )
        )
        return risultato

    if categoria == "recesso":
        if prodotto_soggetto_esclusione and sigillo_integro is None:
            risultato.update(
                _decisione(
                    "chiedi_stato_sigillo",
                    "Per rasoi e spazzolini il recesso dipende dall'integrità del sigillo (policy §3).",
                    "Chiedere se confezione e sigillo sono integri e se il prodotto è stato usato.",
                    rule_id="hygiene_seal_required",
                    sections=("§3",),
                    missing_information=["seal_status"],
                )
            )
            return risultato
        if prodotto_escluso:
            risultato.update(
                _decisione(
                    "rifiuta_recesso_prodotto_escluso",
                    "Rasoio o spazzolino aperto/usato: escluso dal recesso; resta valida la garanzia per difetti (policy §3).",
                    "Comunicare il rifiuto del recesso e ricordare la copertura per eventuali difetti.",
                    rule_id="opened_hygiene_product_excluded",
                    sections=("§3",),
                )
            )
            return risultato
        if entro_finestra is False:
            risultato.update(
                _decisione(
                    "rifiuta_fuori_finestra",
                    f"Sono trascorsi {giorni} giorni dalla consegna, oltre i {FINESTRA_RECESSO_GIORNI} previsti (policy §1).",
                    "Comunicare che la finestra di recesso è scaduta.",
                    rule_id="withdrawal_window_expired",
                    sections=("§1",),
                )
            )
            return risultato
        if entro_finestra is None:
            risultato.update(
                _decisione(
                    "escalation_operatore",
                    "Data di consegna assente: la finestra di recesso non è verificabile (policy §1).",
                    "Controllare manualmente il tracking.",
                    rule_id="withdrawal_delivery_date_missing",
                    sections=("§1", "§8"),
                    missing_information=["delivery_date"],
                    escalation_reason="missing_delivery_date",
                )
            )
            return risultato
        risultato.update(
            _decisione(
                "procedi_rimborso",
                f"Recesso entro i termini ({giorni} giorni) e prodotto non escluso (policy §1, §2).",
                "Creare l'etichetta; comunicarne il costo da detrarre e applicarla solo sull'imballo esterno. Rimborsare dopo il controllo.",
                rule_id="withdrawal_eligible",
                sections=("§1", "§2", "§4", "§6"),
                shipping_payer="customer",
                deduct_shipping_from_refund=True,
                customer_instructions=["external_packaging", "keep_product_box_resellable"],
                physical_validation_required=True,
                timeout_days=POLICY["return_logistics"]["unshipped_label_close_days"],
            )
        )
        return risultato

    if categoria == "arrivato_rotto":
        risultato.update(
            _decisione(
                "procedi_rimborso",
                "Danno da trasporto documentato: rimborso e spedizione a carico azienda (policy §2, §5).",
                "Creare il reso a carico azienda; rimborsare solo dopo il controllo fisico.",
                rule_id="transit_damage_documented",
                sections=("§2", "§5", "§6"),
                shipping_payer="company",
                evidence_status="received",
                physical_validation_required=True,
            )
        )
        return risultato

    if categoria == "doa":
        if entro_finestra is None:
            risultato.update(
                _decisione(
                    "escalation_operatore",
                    "DOA documentato ma data di consegna assente: non si può scegliere tra rimborso e swap (policy §1).",
                    "Verificare manualmente la consegna.",
                    rule_id="doa_delivery_date_missing",
                    sections=("§1", "§8"),
                    missing_information=["delivery_date"],
                    escalation_reason="missing_delivery_date",
                )
            )
            return risultato
        warranty_max_days = POLICY["defective_product"].get("warranty_max_days")
        if warranty_max_days is not None and giorni > warranty_max_days:
            risultato.update(
                _decisione(
                    "rifiuta_fuori_garanzia",
                    f"Sono trascorsi {giorni} giorni dalla consegna, oltre i {warranty_max_days} previsti per la garanzia (policy §1).",
                    "Comunicare che la garanzia è scaduta o valutare manualmente un’eccezione.",
                    rule_id="warranty_window_expired",
                    sections=("§1", "§8"),
                )
            )
            return risultato
        risultato.update(
            _decisione(
                "procedi_swap",
                f"Difetto documentato entro la garanzia di {warranty_max_days} giorni: è previsto lo swap; rimborso solo senza disponibilità (policy §1, §2, §6).",
                "Verificare disponibilità del sostitutivo; spedizione a carico azienda e controllo fisico obbligatorio.",
                rule_id="doa_warranty_swap",
                sections=("§1", "§2", "§5", "§6"),
                shipping_payer="company",
                evidence_status="received",
                replacement_value="equal_or_higher",
                refund_if_no_replacement_stock=True,
                physical_validation_required=True,
            )
        )
        return risultato

    risultato.update(
        _decisione(
            "escalation_operatore",
            f"Categoria '{categoria}' non coperta da una regola confermata.",
            "Valutazione manuale da parte dell'operatore.",
            rule_id="unhandled_category",
            sections=("§8",),
            escalation_reason="unhandled_category",
        )
    )
    return risultato


# --------------------------------------------------------------------------- #
# Test isolato: scenari con dati finti (nessuna chiamata a Shopify o file).
# --------------------------------------------------------------------------- #

def _ordine_finto(titolo: str, stato: str = "fulfilled", totale: str = "150.00") -> dict:
    """Costruisce un dict-ordine finto nella forma di shopify_client.get_order."""
    return {
        "numero_ordine": "#TEST",
        "stato_evasione": stato,
        "prodotti": [{"titolo": titolo, "quantita": 1, "prezzo": totale}],
        "totale_ordine": totale,
        "email_cliente": None,
    }


def _run_scenari() -> None:
    OGGI = date(2026, 7, 21)  # data di riferimento fissa per test deterministici

    scenari = [
        (
            "Recesso entro finestra su asciugacapelli (4 gg) -> procedi_rimborso",
            dict(
                categoria="recesso",
                numero_ordine="#1002",
                dati_ordine=_ordine_finto("asciugacapelli Laifen"),
                delivery_info={"tracking": "TEST1002IT", "delivered_at": "2026-07-17"},
            ),
        ),
        (
            "Recesso su rasoio -> rifiuta_recesso_prodotto_escluso",
            dict(
                categoria="recesso",
                numero_ordine="#1005",
                dati_ordine=_ordine_finto("Rasoio elettrico Laifen"),
                delivery_info={"tracking": "TEST1005IT", "delivered_at": "2026-07-16"},
                sigillo_integro=False,
            ),
        ),
        (
            "Recesso a 28 giorni -> rifiuta_fuori_finestra",
            dict(
                categoria="recesso",
                numero_ordine="#1003",
                dati_ordine=_ordine_finto("asciugacapelli Laifen"),
                delivery_info={"tracking": "TEST1003IT", "delivered_at": "2026-06-23"},
            ),
        ),
        (
            "Ticket senza numero d'ordine -> chiedi_numero_ordine",
            dict(
                categoria="doa",
                numero_ordine=None,
                dati_ordine=None,
                delivery_info={"tracking": None, "delivered_at": None},
            ),
        ),
        (
            "DOA senza prove (MVP) -> chiedi_foto_video",
            dict(
                categoria="doa",
                numero_ordine="#1001",
                dati_ordine=_ordine_finto("asciugacapelli Laifen"),
                delivery_info={"tracking": "TEST1001IT", "delivered_at": "2026-07-17"},
            ),
        ),
        (
            "DOA con prove, entro 2 anni -> procedi_swap",
            dict(
                categoria="doa",
                numero_ordine="#1001",
                dati_ordine=_ordine_finto("asciugacapelli Laifen"),
                delivery_info={"tracking": "TEST1001IT", "delivered_at": "2026-07-17"},
                prove_fornite=True,
            ),
        ),
        (
            "Escalation (categoria altro, es. chargeback) -> escalation_operatore",
            dict(
                categoria="altro",
                numero_ordine="#1007",
                dati_ordine=_ordine_finto("prodotto premium", totale="600.00"),
                delivery_info={"tracking": "TEST1007IT", "delivered_at": "2026-07-11"},
            ),
        ),
    ]

    for titolo, kwargs in scenari:
        decisione = applica_regole(oggi=OGGI, **kwargs)
        print("=" * 78)
        print(titolo)
        print("-" * 78)
        print(json.dumps(decisione, ensure_ascii=False, indent=2))
    print("=" * 78)


if __name__ == "__main__":
    # Console Windows: forza UTF-8 così i caratteri come § si stampano corretti.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    _run_scenari()
    sys.exit(0)
