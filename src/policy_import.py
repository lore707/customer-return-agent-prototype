"""Importazione sicura di documenti policy in una bozza strutturata.

L'estrazione locale serve alla sandbox portfolio: rende espliciti i campi da
confermare senza modificare la policy attiva. PDF e DOCX vengono letti solo
quando le relative dipendenze sono installate.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests

MAX_DOCUMENT_BYTES = 2_000_000
ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx"}


class PolicyImportError(ValueError):
    """Errore mostrabile all'utente durante l'importazione."""


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


def _plain_html(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    return "\n".join(parser.parts)


def document_text(filename: str, payload: bytes, content_type: str = "") -> str:
    if not payload:
        raise PolicyImportError("Il documento è vuoto.")
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise PolicyImportError("Il documento supera il limite di 2 MB.")

    suffix = Path(filename or "policy.txt").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS and "html" not in content_type:
        raise PolicyImportError("Formato non supportato. Usa PDF, DOCX, TXT o MD.")

    try:
        if suffix == ".pdf" or content_type == "application/pdf":
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(payload))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        elif suffix == ".docx" or "wordprocessingml" in content_type:
            from docx import Document

            document = Document(BytesIO(payload))
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            for table in document.tables:
                for row in table.rows:
                    text += "\n" + " | ".join(cell.text for cell in row.cells)
        else:
            text = payload.decode("utf-8-sig", errors="replace")
            if "html" in content_type:
                text = _plain_html(text)
    except PolicyImportError:
        raise
    except Exception as exc:
        raise PolicyImportError("Non è stato possibile leggere il documento.") from exc

    if len(text.strip()) < 40:
        raise PolicyImportError("Il documento non contiene abbastanza testo analizzabile.")
    return text.strip()


def _validate_public_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise PolicyImportError("Inserisci un URL HTTPS pubblico.")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        }
    except socket.gaierror as exc:
        raise PolicyImportError("Il dominio indicato non è raggiungibile.") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if any(
            (
                ip.is_private,
                ip.is_loopback,
                ip.is_link_local,
                ip.is_multicast,
                ip.is_reserved,
                ip.is_unspecified,
            )
        ):
            raise PolicyImportError("L'URL deve puntare a una risorsa pubblica.")
    return value.strip()


def url_text(value: str) -> tuple[str, str]:
    url = _validate_public_url(value)
    try:
        response = requests.get(
            url,
            allow_redirects=False,
            stream=True,
            timeout=(3.05, 10),
            headers={"User-Agent": "ReturnPolicyImporter/1.0"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise PolicyImportError("Non è stato possibile scaricare la policy.") from exc
    if 300 <= response.status_code < 400:
        raise PolicyImportError("I redirect non sono supportati: usa l'URL finale.")

    try:
        declared_size = int(response.headers.get("content-length") or 0)
    except ValueError:
        declared_size = 0
    if declared_size > MAX_DOCUMENT_BYTES:
        raise PolicyImportError("Il documento supera il limite di 2 MB.")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(65_536):
        size += len(chunk)
        if size > MAX_DOCUMENT_BYTES:
            raise PolicyImportError("Il documento supera il limite di 2 MB.")
        chunks.append(chunk)

    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    filename = Path(urlparse(url).path).name or (
        "policy.html" if "html" in content_type else "policy.txt"
    )
    return document_text(filename, b"".join(chunks), content_type), url


def _contains(text: str, *terms: str) -> bool:
    return any(term in text for term in terms)


def extract_structured_rules(text: str) -> dict:
    """Converte il testo in una bozza esplicita da revisionare manualmente."""

    cleaned = text.strip()
    if len(cleaned) < 40:
        raise PolicyImportError("Inserisci almeno 40 caratteri di policy.")
    lowered = cleaned.casefold()
    day_match = re.search(r"\b(\d{1,3})\s*(?:giorni|giorno|gg)\b", lowered)
    window = f"{day_match.group(1)} giorni" if day_match else "Da confermare"

    starts_from = (
        "Data di consegna / tracking"
        if _contains(lowered, "data di consegna", "dalla consegna", "tracking")
        else "Da confermare"
    )
    condition = (
        "Integro e rivendibile"
        if _contains(lowered, "rivendibile", "stesse condizioni", "integro")
        else "Da confermare"
    )
    if "a carico del cliente" in lowered and "a carico azienda" in lowered:
        shipping = "Cliente per recesso · Azienda per difetti"
    elif "a carico del cliente" in lowered:
        shipping = "A carico del cliente"
    elif _contains(lowered, "a carico azienda", "a carico dell'azienda"):
        shipping = "A carico dell’azienda"
    else:
        shipping = "Da confermare"

    hygiene = (
        "Aperti: non idonei · Sigillati: idonei"
        if _contains(lowered, "rasoi", "rasoio", "spazzolini", "spazzolino")
        and _contains(lowered, "aperta", "aperto", "sigillo")
        else "Non specificato"
    )
    evidence = (
        "Foto e video richiesti"
        if "foto" in lowered and "video" in lowered
        else "Da confermare"
    )
    refund = (
        "Solo dopo il controllo fisico"
        if "rimborso" in lowered
        and _contains(lowered, "dopo che", "dopo il controllo", "verificat")
        else "Da confermare"
    )
    approval = (
        "Approvazione operatore obbligatoria"
        if "operatore" in lowered
        and _contains(lowered, "approva", "non invia mai", "revisione umana")
        else "Da confermare"
    )

    values = [
        ("return_window", "Finestra recesso", window),
        ("starts_from", "Decorrenza", starts_from),
        ("product_condition", "Condizione prodotto", condition),
        ("return_shipping", "Spedizione di ritorno", shipping),
        ("hygiene_products", "Prodotti igienici aperti", hygiene),
        ("doa_evidence", "Prove DOA", evidence),
        ("refund_timing", "Avvio rimborso", refund),
        ("human_gate", "Approvazione finale", approval),
    ]
    rules = [
        {
            "id": rule_id,
            "label": label,
            "value": value,
            "confidence": 0.94 if value not in {"Da confermare", "Non specificato"} else 0.58,
            "needs_confirmation": value in {"Da confermare", "Non specificato"},
        }
        for rule_id, label, value in values
    ]

    confirmations = []
    for line in cleaned.splitlines():
        if "DA CONFERMARE" not in line.upper():
            continue
        description = re.sub(r"[`*#\[\]]", "", line).strip(" -:—")
        if description and description not in confirmations:
            confirmations.append(description)
    if not confirmations:
        confirmations = [
            f"{item['label']}: il documento non definisce un valore univoco."
            for item in rules
            if item["needs_confirmation"]
        ]

    return {
        "rules": rules,
        "confirmations": confirmations[:7],
        "rule_count": len(rules),
        "confirmation_count": min(len(confirmations), 7),
        "engine": "structured_extraction_preview",
        "characters_read": len(cleaned),
    }
