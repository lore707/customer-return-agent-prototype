"""Dominio minimo per una pratica di reso persistente."""

from enum import StrEnum


class CaseStatus(StrEnum):
    NEW = "NEW"
    ANALYZED = "ANALYZED"
    NEEDS_INFORMATION = "NEEDS_INFORMATION"
    WAITING_HUMAN_APPROVAL = "WAITING_HUMAN_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    LABEL_CREATED = "LABEL_CREATED"
    WAITING_FOR_RETURN = "WAITING_FOR_RETURN"
    RETURN_IN_TRANSIT = "RETURN_IN_TRANSIT"
    RETURN_RECEIVED = "RETURN_RECEIVED"
    RETURN_VALIDATED = "RETURN_VALIDATED"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"
    REPLACEMENT_PENDING = "REPLACEMENT_PENDING"
    REPLACED = "REPLACED"
    CLOSED = "CLOSED"
    ESCALATED = "ESCALATED"


ALLOWED_TRANSITIONS = {
    CaseStatus.NEW: {CaseStatus.ANALYZED},
    CaseStatus.ANALYZED: {
        CaseStatus.NEEDS_INFORMATION,
        CaseStatus.WAITING_HUMAN_APPROVAL,
        CaseStatus.ESCALATED,
    },
    CaseStatus.NEEDS_INFORMATION: {
        CaseStatus.WAITING_HUMAN_APPROVAL,
        CaseStatus.ESCALATED,
        CaseStatus.CLOSED,
    },
    CaseStatus.WAITING_HUMAN_APPROVAL: {
        CaseStatus.APPROVED,
        CaseStatus.REJECTED,
        CaseStatus.ESCALATED,
        CaseStatus.NEEDS_INFORMATION,
    },
    CaseStatus.APPROVED: {CaseStatus.LABEL_CREATED, CaseStatus.CLOSED},
    CaseStatus.LABEL_CREATED: {CaseStatus.WAITING_FOR_RETURN},
    CaseStatus.WAITING_FOR_RETURN: {
        CaseStatus.RETURN_IN_TRANSIT,
        CaseStatus.RETURN_RECEIVED,
        CaseStatus.CLOSED,
    },
    CaseStatus.RETURN_IN_TRANSIT: {CaseStatus.RETURN_RECEIVED},
    CaseStatus.RETURN_RECEIVED: {
        CaseStatus.RETURN_VALIDATED,
        CaseStatus.REJECTED,
        CaseStatus.ESCALATED,
        CaseStatus.CLOSED,
    },
    CaseStatus.RETURN_VALIDATED: {
        CaseStatus.REFUND_PENDING,
        CaseStatus.REPLACEMENT_PENDING,
        CaseStatus.CLOSED,
    },
    CaseStatus.REFUND_PENDING: {CaseStatus.REFUNDED},
    CaseStatus.REFUNDED: {CaseStatus.CLOSED},
    CaseStatus.REPLACEMENT_PENDING: {CaseStatus.REPLACED},
    CaseStatus.REPLACED: {CaseStatus.CLOSED},
    CaseStatus.REJECTED: {CaseStatus.CLOSED},
    CaseStatus.ESCALATED: {CaseStatus.CLOSED},
    CaseStatus.CLOSED: set(),
}


STATUS_LABELS = {
    CaseStatus.NEW.value: "Nuova",
    CaseStatus.ANALYZED.value: "Analizzata",
    CaseStatus.NEEDS_INFORMATION.value: "Informazioni richieste",
    CaseStatus.WAITING_HUMAN_APPROVAL.value: "Attesa approvazione",
    CaseStatus.APPROVED.value: "Approvata",
    CaseStatus.REJECTED.value: "Respinta",
    CaseStatus.LABEL_CREATED.value: "Etichetta creata",
    CaseStatus.WAITING_FOR_RETURN.value: "Attesa spedizione reso",
    CaseStatus.RETURN_IN_TRANSIT.value: "Reso in transito",
    CaseStatus.RETURN_RECEIVED.value: "Arrivato - da controllare",
    CaseStatus.RETURN_VALIDATED.value: "Controllato e valido",
    CaseStatus.REFUND_PENDING.value: "Rimborso in attesa",
    CaseStatus.REFUNDED.value: "Rimborsato",
    CaseStatus.REPLACEMENT_PENDING.value: "Swap in attesa",
    CaseStatus.REPLACED.value: "Sostituito",
    CaseStatus.CLOSED.value: "Chiuso",
    CaseStatus.ESCALATED.value: "Escalato",
}


RETURN_TYPE_BY_CATEGORY = {
    "recesso": "right_of_withdrawal",
    "doa": "defective_product",
    "arrivato_rotto": "damaged_product",
    "articolo_errato": "wrong_item",
    "altro": "escalation",
}

NEEDS_INFORMATION_OUTCOMES = {
    "chiedi_numero_ordine",
    "chiedi_foto_video",
    "chiedi_stato_sigillo",
    "offri_scelta_rimborso_o_swap",
    "ordine_non_consegnato",
}

REJECTION_OUTCOMES = {
    "rifiuta_recesso_prodotto_escluso",
    "rifiuta_fuori_finestra",
}

LABEL_OUTCOMES = {"procedi_rimborso", "procedi_swap"}


class InvalidTransition(ValueError):
    """La transizione richiesta non è prevista dal workflow."""


def validate_transition(current: str, target: str) -> None:
    current_status = CaseStatus(current)
    target_status = CaseStatus(target)
    if target_status not in ALLOWED_TRANSITIONS[current_status]:
        raise InvalidTransition(f"Transizione non consentita: {current} -> {target}")


def eligibility_for_outcome(outcome: str) -> str:
    if outcome in LABEL_OUTCOMES:
        return "eligible"
    if outcome in REJECTION_OUTCOMES:
        return "not_eligible"
    if outcome in NEEDS_INFORMATION_OUTCOMES:
        return "needs_information"
    return "manual_review"
