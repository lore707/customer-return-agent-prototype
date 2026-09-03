"""Persistenza SQLite semplice per ReturnCase e timeline."""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import domain

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DATABASE_PATH = ROOT / "data" / "returns.db"

CASE_FIELDS = {
    "session_id",
    "shopify_order_id",
    "shopify_order_number",
    "customer_id",
    "customer_name",
    "customer_email",
    "product_name",
    "sku",
    "variant_id",
    "line_item_id",
    "quantity",
    "purchase_date",
    "delivery_date",
    "request_date",
    "return_type",
    "return_reason",
    "detailed_reason",
    "customer_message",
    "ai_classification",
    "confidence",
    "eligibility_result",
    "policy_applied",
    "policy_decision",
    "suggested_resolution",
    "original_suggested_response",
    "final_response",
    "human_decision",
    "human_reason",
    "label_status",
    "shipping_provider",
    "sendcloud_return_id",
    "tracking_number",
    "label_url",
    "refund_status",
    "replacement_status",
    "assigned_operator",
    "analysis_duration_ms",
    "operator_decision_seconds",
    "api_action_count",
    "manual_step_count",
    "data_source",
    "source_mode",
    "source_fetched_at",
    "source_payload",
    "ai_mode",
    "scenario_slug",
}

MIGRATION_COLUMNS = {
    "analysis_duration_ms": "INTEGER",
    "operator_decision_seconds": "INTEGER",
    "api_action_count": "INTEGER NOT NULL DEFAULT 0",
    "manual_step_count": "INTEGER NOT NULL DEFAULT 0",
    "data_source": "TEXT",
    "source_mode": "TEXT",
    "source_fetched_at": "TEXT",
    "source_payload": "TEXT NOT NULL DEFAULT '{}'",
    "ai_mode": "TEXT",
    "scenario_slug": "TEXT",
    "policy_decision": "TEXT NOT NULL DEFAULT '{}'",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def database_path() -> Path:
    configured = os.getenv("DATABASE_PATH")
    return Path(configured).expanduser() if configured else DEFAULT_DATABASE_PATH


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else database_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session(path: str | Path | None = None):
    """Apre una connessione, gestisce la transazione e la chiude sempre."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database(path: str | Path | None = None) -> None:
    with session(path) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(return_cases)")
        }
        for column, definition in MIGRATION_COLUMNS.items():
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE return_cases ADD COLUMN {column} {definition}"
                )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_return_cases_scenario
               ON return_cases(scenario_slug)"""
        )
        # Migrazione semplice dei casi creati prima dell'introduzione della
        # memoria conversazionale: il messaggio originale diventa il primo
        # elemento dello storico, senza duplicarlo ai riavvii successivi.
        conn.execute(
            """INSERT INTO case_messages
               (case_id, session_id, customer_id, customer_email, role, channel,
                message_type, message, metadata, created_at)
               SELECT rc.id, COALESCE(rc.session_id, 'legacy-' || rc.id),
                      rc.customer_id, rc.customer_email, 'cliente', 'dashboard',
                      'customer_request', rc.customer_message, '{}', rc.created_at
               FROM return_cases rc
               WHERE NOT EXISTS (
                   SELECT 1 FROM case_messages cm WHERE cm.case_id = rc.id
               )"""
        )


def _new_case_id() -> str:
    return f"RC-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def _json_value(value) -> str:
    return json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value


def _add_event(
    conn: sqlite3.Connection,
    case_id: str,
    event_type: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    details: dict | None = None,
) -> None:
    conn.execute(
        """INSERT INTO audit_events
           (case_id, event_type, from_status, to_status, details, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            case_id,
            event_type,
            from_status,
            to_status,
            json.dumps(details or {}, ensure_ascii=False),
            utc_now(),
        ),
    )


def create_case(data: dict, path: str | Path | None = None) -> dict:
    now = utc_now()
    case_id = data.get("id") or _new_case_id()
    values = {
        **data,
        "id": case_id,
        "status": domain.CaseStatus.NEW.value,
        "created_at": now,
        "updated_at": now,
    }
    values["ai_classification"] = _json_value(values.get("ai_classification", {}))
    values["source_payload"] = _json_value(values.get("source_payload", {}))
    values["policy_decision"] = _json_value(values.get("policy_decision", {}))
    columns = [
        "id",
        *sorted(CASE_FIELDS),
        "status",
        "created_at",
        "updated_at",
    ]
    columns = [column for column in columns if column in values]
    placeholders = ", ".join("?" for _ in columns)
    with session(path) as conn:
        conn.execute(
            f"INSERT INTO return_cases ({', '.join(columns)}) VALUES ({placeholders})",
            [values[column] for column in columns],
        )
        _add_event(
            conn,
            case_id,
            "customer_request_received",
            to_status=domain.CaseStatus.NEW.value,
            details={"message": data.get("customer_message", "")},
        )
        linked = conn.execute(
            """UPDATE case_messages
               SET case_id = ?,
                   customer_id = COALESCE(customer_id, ?),
                   customer_email = COALESCE(customer_email, ?)
               WHERE session_id = ? AND case_id IS NULL""",
            (
                case_id,
                data.get("customer_id"),
                data.get("customer_email"),
                data.get("session_id") or f"case-{case_id}",
            ),
        ).rowcount
        if linked == 0 and data.get("customer_message"):
            conn.execute(
                """INSERT INTO case_messages
                   (case_id, session_id, customer_id, customer_email, role,
                    channel, message_type, message, metadata, created_at)
                   VALUES (?, ?, ?, ?, 'cliente', 'dashboard',
                           'customer_request', ?, '{}', ?)""",
                (
                    case_id,
                    data.get("session_id") or f"case-{case_id}",
                    data.get("customer_id"),
                    data.get("customer_email"),
                    data["customer_message"],
                    now,
                ),
            )
    return get_case(case_id, path)


def get_case(case_id: str, path: str | Path | None = None) -> dict | None:
    with session(path) as conn:
        row = conn.execute(
            "SELECT * FROM return_cases WHERE id = ?", (case_id,)
        ).fetchone()
    return _row_to_case(row) if row else None


def find_open_case(
    order_number: str | None,
    customer_id: str | None = None,
    *,
    path: str | Path | None = None,
) -> dict | None:
    """Trova la pratica aperta piu recente per ordine/cliente."""
    if not order_number and not customer_id:
        return None
    where = ["status != 'CLOSED'"]
    params: list[str] = []
    if order_number:
        where.append("shopify_order_number = ?")
        params.append(str(order_number).lstrip("#"))
    if customer_id:
        where.append("customer_id = ?")
        params.append(str(customer_id))
    with session(path) as conn:
        row = conn.execute(
            f"SELECT * FROM return_cases WHERE {' AND '.join(where)} "
            "ORDER BY updated_at DESC LIMIT 1",
            params,
        ).fetchone()
    return _row_to_case(row) if row else None


def _row_to_case(row: sqlite3.Row) -> dict:
    result = dict(row)
    for field in ("ai_classification", "source_payload", "policy_decision"):
        try:
            result[field] = json.loads(result.get(field) or "{}")
        except json.JSONDecodeError:
            result[field] = {}
    return result


def get_case_by_scenario(
    scenario_slug: str, path: str | Path | None = None
) -> dict | None:
    """Restituisce la pratica portfolio associata a uno scenario guidato."""
    with session(path) as conn:
        row = conn.execute(
            """SELECT * FROM return_cases WHERE scenario_slug = ?
               ORDER BY created_at DESC LIMIT 1""",
            (scenario_slug,),
        ).fetchone()
    return _row_to_case(row) if row else None


def clear_demo_cases(path: str | Path | None = None) -> int:
    """Rimuove solo i record portfolio, senza toccare pratiche create a mano."""
    with session(path) as conn:
        cursor = conn.execute(
            "DELETE FROM return_cases WHERE scenario_slug IS NOT NULL"
        )
        return cursor.rowcount


def list_cases(
    *,
    status: str | None = None,
    reason: str | None = None,
    query: str | None = None,
    limit: int = 100,
    path: str | Path | None = None,
) -> list[dict]:
    where = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if reason:
        where.append("return_reason = ?")
        params.append(reason)
    if query:
        where.append(
            "(shopify_order_number LIKE ? OR customer_name LIKE ? "
            "OR customer_email LIKE ? OR product_name LIKE ? OR sku LIKE ?)"
        )
        like = f"%{query}%"
        params.extend([like] * 5)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(max(1, min(limit, 500)))
    with session(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM return_cases {clause} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_case(row) for row in rows]


def add_message(
    session_id: str,
    role: str,
    message: str,
    *,
    case_id: str | None = None,
    customer_id: str | None = None,
    customer_email: str | None = None,
    channel: str = "dashboard",
    message_type: str = "message",
    metadata: dict | None = None,
    path: str | Path | None = None,
) -> dict:
    """Salva un messaggio della conversazione associabile a una pratica."""
    if role not in {"cliente", "agente", "operatore", "sistema"}:
        raise ValueError(f"Ruolo messaggio non valido: {role}")
    text = str(message or "").strip()
    if not text:
        raise ValueError("Il messaggio non puo essere vuoto.")
    created_at = utc_now()
    with session(path) as conn:
        cursor = conn.execute(
            """INSERT INTO case_messages
               (case_id, session_id, customer_id, customer_email, role, channel,
                message_type, message, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                case_id,
                session_id,
                customer_id,
                customer_email,
                role,
                channel,
                message_type,
                text,
                json.dumps(metadata or {}, ensure_ascii=False),
                created_at,
            ),
        )
        message_id = cursor.lastrowid
    return {
        "id": message_id,
        "case_id": case_id,
        "session_id": session_id,
        "role": role,
        "channel": channel,
        "message_type": message_type,
        "message": text,
        "metadata": metadata or {},
        "created_at": created_at,
    }


def link_session_messages(
    session_id: str,
    case_id: str,
    *,
    customer_id: str | None = None,
    customer_email: str | None = None,
    path: str | Path | None = None,
) -> None:
    """Collega alla pratica i messaggi raccolti prima di conoscere l'ordine."""
    with session(path) as conn:
        conn.execute(
            """UPDATE case_messages
               SET case_id = ?,
                   customer_id = COALESCE(customer_id, ?),
                   customer_email = COALESCE(customer_email, ?)
               WHERE session_id = ? AND case_id IS NULL""",
            (case_id, customer_id, customer_email, session_id),
        )


def _message_row(row: sqlite3.Row) -> dict:
    result = dict(row)
    try:
        result["metadata"] = json.loads(result.get("metadata") or "{}")
    except json.JSONDecodeError:
        result["metadata"] = {}
    return result


def get_case_messages(
    case_id: str, path: str | Path | None = None
) -> list[dict]:
    with session(path) as conn:
        rows = conn.execute(
            """SELECT * FROM case_messages
               WHERE case_id = ? ORDER BY created_at, id""",
            (case_id,),
        ).fetchall()
    return [_message_row(row) for row in rows]


def get_session_messages(
    session_id: str, path: str | Path | None = None
) -> list[dict]:
    with session(path) as conn:
        rows = conn.execute(
            """SELECT * FROM case_messages
               WHERE session_id = ? ORDER BY created_at, id""",
            (session_id,),
        ).fetchall()
    return [_message_row(row) for row in rows]


def message_counts(
    case_ids: list[str], path: str | Path | None = None
) -> dict[str, int]:
    if not case_ids:
        return {}
    placeholders = ", ".join("?" for _ in case_ids)
    with session(path) as conn:
        rows = conn.execute(
            f"""SELECT case_id, COUNT(*) AS count FROM case_messages
                 WHERE case_id IN ({placeholders}) GROUP BY case_id""",
            case_ids,
        ).fetchall()
    return {row["case_id"]: row["count"] for row in rows}


def add_audit_event(
    case_id: str,
    event_type: str,
    *,
    details: dict | None = None,
    path: str | Path | None = None,
) -> None:
    with session(path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM return_cases WHERE id = ?", (case_id,)
        ).fetchone()
        if not exists:
            raise KeyError(case_id)
        _add_event(conn, case_id, event_type, details=details)


def update_case(
    case_id: str,
    updates: dict,
    *,
    event_type: str | None = None,
    event_details: dict | None = None,
    path: str | Path | None = None,
) -> dict:
    clean = {key: value for key, value in updates.items() if key in CASE_FIELDS}
    if not clean:
        existing = get_case(case_id, path)
        if existing is None:
            raise KeyError(case_id)
        return existing
    if "ai_classification" in clean:
        clean["ai_classification"] = _json_value(clean["ai_classification"])
    if "source_payload" in clean:
        clean["source_payload"] = _json_value(clean["source_payload"])
    if "policy_decision" in clean:
        clean["policy_decision"] = _json_value(clean["policy_decision"])
    clean["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in clean)
    with session(path) as conn:
        cursor = conn.execute(
            f"UPDATE return_cases SET {assignments} WHERE id = ?",
            [*clean.values(), case_id],
        )
        if cursor.rowcount == 0:
            raise KeyError(case_id)
        if event_type:
            _add_event(conn, case_id, event_type, details=event_details or updates)
    return get_case(case_id, path)


def transition_case(
    case_id: str,
    target_status: str,
    *,
    event_type: str,
    details: dict | None = None,
    path: str | Path | None = None,
) -> dict:
    with session(path) as conn:
        row = conn.execute(
            "SELECT status FROM return_cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise KeyError(case_id)
        current = row["status"]
        domain.validate_transition(current, target_status)
        now = utc_now()
        closed_at = now if target_status == domain.CaseStatus.CLOSED.value else None
        conn.execute(
            """UPDATE return_cases
               SET status = ?, updated_at = ?, closed_at = COALESCE(?, closed_at)
               WHERE id = ?""",
            (target_status, now, closed_at, case_id),
        )
        _add_event(
            conn,
            case_id,
            event_type,
            from_status=current,
            to_status=target_status,
            details=details,
        )
    return get_case(case_id, path)


def get_timeline(case_id: str, path: str | Path | None = None) -> list[dict]:
    with session(path) as conn:
        rows = conn.execute(
            """SELECT event_type, from_status, to_status, details, created_at
               FROM audit_events WHERE case_id = ? ORDER BY id""",
            (case_id,),
        ).fetchall()
    events = []
    for row in rows:
        event = dict(row)
        try:
            event["details"] = json.loads(event["details"] or "{}")
        except json.JSONDecodeError:
            event["details"] = {}
        events.append(event)
    return events


def add_operator_feedback(
    case_id: str,
    feedback_type: str,
    *,
    reason_tag: str | None = None,
    instructions: str | None = None,
    original_draft: str | None = None,
    revised_draft: str | None = None,
    path: str | Path | None = None,
) -> dict:
    """Registra perché l'operatore modifica, rigenera o scarta una bozza."""
    created_at = utc_now()
    with session(path) as conn:
        row = conn.execute(
            "SELECT return_reason FROM return_cases WHERE id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise KeyError(case_id)
        cursor = conn.execute(
            """INSERT INTO operator_feedback
               (case_id, category, feedback_type, reason_tag, instructions,
                original_draft, revised_draft, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                case_id,
                row["return_reason"],
                feedback_type,
                reason_tag,
                instructions,
                original_draft,
                revised_draft,
                created_at,
            ),
        )
        feedback_id = cursor.lastrowid
        _add_event(
            conn,
            case_id,
            "operator_feedback_recorded",
            details={
                "feedback_id": feedback_id,
                "feedback_type": feedback_type,
                "reason_tag": reason_tag,
            },
        )
    return {
        "id": feedback_id,
        "case_id": case_id,
        "feedback_type": feedback_type,
        "reason_tag": reason_tag,
        "instructions": instructions,
        "created_at": created_at,
    }


def get_case_feedback(
    case_id: str, path: str | Path | None = None
) -> list[dict]:
    with session(path) as conn:
        rows = conn.execute(
            """SELECT * FROM operator_feedback
               WHERE case_id = ? ORDER BY id DESC""",
            (case_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def feedback_examples(
    category: str, *, limit: int = 3, path: str | Path | None = None
) -> list[dict]:
    """Piccoli esempi approvati riusabili durante una rigenerazione live."""
    with session(path) as conn:
        rows = conn.execute(
            """SELECT reason_tag, instructions, original_draft, revised_draft
               FROM operator_feedback
               WHERE category = ? AND revised_draft IS NOT NULL
               ORDER BY id DESC LIMIT ?""",
            (category, max(1, min(limit, 5))),
        ).fetchall()
    return [dict(row) for row in rows]


def apply_policy_timeouts(
    *,
    no_response_days: int,
    unshipped_days: int,
    now: datetime | None = None,
    path: str | Path | None = None,
) -> dict:
    """Applica le due scadenze automatiche confermate in policies.md.

    Chiude i casi senza prove e annulla le etichette mai spedite. Ogni
    variazione viene registrata nell'audit log.
    """
    current_time = now or datetime.now(timezone.utc)
    closed_no_response = 0
    closed_unshipped = 0
    with session(path) as conn:
        rows = conn.execute(
            """SELECT id, status, created_at FROM return_cases
               WHERE status = 'NEEDS_INFORMATION'
                 AND suggested_resolution = 'chiedi_foto_video'
                 AND scenario_slug IS NULL"""
        ).fetchall()
        for row in rows:
            opened = datetime.fromisoformat(row["created_at"])
            if (current_time - opened).total_seconds() < no_response_days * 86400:
                continue
            timestamp = current_time.isoformat(timespec="seconds")
            conn.execute(
                """UPDATE return_cases SET status = 'CLOSED', updated_at = ?,
                   closed_at = ? WHERE id = ?""",
                (timestamp, timestamp, row["id"]),
            )
            _add_event(
                conn,
                row["id"],
                "evidence_timeout_closed",
                from_status=row["status"],
                to_status=domain.CaseStatus.CLOSED.value,
                details={"timeout_days": no_response_days, "policy_section": "§5"},
            )
            closed_no_response += 1

        rows = conn.execute(
            """SELECT rc.id, rc.status, rc.created_at,
                      MAX(CASE WHEN ae.event_type = 'return_label_generated'
                               THEN ae.created_at END) AS label_created_at
               FROM return_cases rc
               LEFT JOIN audit_events ae ON ae.case_id = rc.id
               WHERE rc.status = 'WAITING_FOR_RETURN' AND rc.label_status = 'created'
                 AND rc.scenario_slug IS NULL
               GROUP BY rc.id"""
        ).fetchall()
        for row in rows:
            label_created = datetime.fromisoformat(
                row["label_created_at"] or row["created_at"]
            )
            if (current_time - label_created).total_seconds() < unshipped_days * 86400:
                continue
            timestamp = current_time.isoformat(timespec="seconds")
            conn.execute(
                """UPDATE return_cases SET status = 'CLOSED', label_status = 'cancelled',
                   updated_at = ?, closed_at = ? WHERE id = ?""",
                (timestamp, timestamp, row["id"]),
            )
            _add_event(
                conn,
                row["id"],
                "unshipped_return_timeout_closed",
                from_status=row["status"],
                to_status=domain.CaseStatus.CLOSED.value,
                details={"timeout_days": unshipped_days, "policy_section": "§7"},
            )
            closed_unshipped += 1
    return {
        "closed_no_response": closed_no_response,
        "closed_unshipped": closed_unshipped,
    }


def analytics(path: str | Path | None = None) -> dict:
    with session(path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM return_cases").fetchone()[0]
        open_count = conn.execute(
            "SELECT COUNT(*) FROM return_cases WHERE status != 'CLOSED'"
        ).fetchone()[0]
        closed = conn.execute(
            "SELECT COUNT(*) FROM return_cases WHERE status = 'CLOSED'"
        ).fetchone()[0]
        waiting = conn.execute(
            """SELECT COUNT(*) FROM return_cases
               WHERE status IN ('NEEDS_INFORMATION', 'WAITING_HUMAN_APPROVAL',
                                'WAITING_FOR_RETURN', 'RETURN_IN_TRANSIT',
                                'RETURN_RECEIVED', 'RETURN_VALIDATED',
                                'REFUND_PENDING', 'REPLACEMENT_PENDING')"""
        ).fetchone()[0]
        escalated = conn.execute(
            "SELECT COUNT(*) FROM return_cases WHERE status = 'ESCALATED'"
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT return_reason, COUNT(*) AS count
               FROM return_cases GROUP BY return_reason ORDER BY count DESC"""
        ).fetchall()
        avg_hours = conn.execute(
            """SELECT AVG((julianday(closed_at) - julianday(created_at)) * 24)
               FROM return_cases WHERE closed_at IS NOT NULL"""
        ).fetchone()[0]
        avg_analysis_ms = conn.execute(
            "SELECT AVG(analysis_duration_ms) FROM return_cases"
        ).fetchone()[0]
        decisions = conn.execute(
            "SELECT COUNT(*) FROM return_cases WHERE human_decision IS NOT NULL"
        ).fetchone()[0]
        modified = conn.execute(
            """SELECT COUNT(*) FROM return_cases
               WHERE human_decision = 'modified_and_approved'"""
        ).fetchone()[0]
    return {
        "total": total,
        "open": open_count,
        "closed": closed,
        "waiting": waiting,
        "escalated": escalated,
        "average_close_hours": round(avg_hours, 1) if avg_hours is not None else None,
        "average_analysis_ms": round(avg_analysis_ms) if avg_analysis_ms is not None else None,
        "modified_suggestion_rate": round(modified * 100 / decisions, 1) if decisions else None,
        "by_reason": [dict(row) for row in rows],
    }


def performance_analytics(path: str | Path | None = None) -> dict:
    """Metriche osservabili per la pagina Analytics, senza dati inventati."""
    with session(path) as conn:
        row = conn.execute(
            """SELECT
                 COUNT(*) AS total,
                 SUM(CASE WHEN original_suggested_response IS NOT NULL THEN 1 ELSE 0 END) AS generated,
                 SUM(CASE WHEN human_decision = 'approved' THEN 1 ELSE 0 END) AS approved,
                 SUM(CASE WHEN human_decision = 'modified_and_approved' THEN 1 ELSE 0 END) AS modified,
                 SUM(CASE WHEN status = 'ESCALATED' THEN 1 ELSE 0 END) AS escalated,
                 AVG(confidence) AS avg_confidence,
                 AVG(analysis_duration_ms) AS avg_generation_ms,
                 AVG(CASE WHEN closed_at IS NOT NULL
                     THEN (julianday(closed_at) - julianday(created_at)) * 24 END) AS avg_resolution_hours,
                 AVG(CASE WHEN closed_at IS NOT NULL
                     THEN ((julianday(closed_at) - julianday(created_at)) * 24) <= 24 END) AS resolved_within_sla
               FROM return_cases"""
        ).fetchone()
        regenerated = conn.execute(
            "SELECT COUNT(*) FROM operator_feedback WHERE feedback_type = 'regenerated'"
        ).fetchone()[0]
        feedback_rows = conn.execute(
            """SELECT COALESCE(reason_tag, 'other') AS reason_tag, COUNT(*) AS count
               FROM operator_feedback GROUP BY COALESCE(reason_tag, 'other')
               ORDER BY count DESC"""
        ).fetchall()
        reason_rows = conn.execute(
            """SELECT return_reason, COUNT(*) AS count FROM return_cases
               GROUP BY return_reason ORDER BY count DESC"""
        ).fetchall()
        case_days = {
            item["day"]: item["count"]
            for item in conn.execute(
                """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count
                   FROM return_cases GROUP BY substr(created_at, 1, 10)"""
            ).fetchall()
        }
        approval_days = {
            item["day"]: item["count"]
            for item in conn.execute(
                """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count
                   FROM audit_events WHERE event_type = 'human_approval_recorded'
                   GROUP BY substr(created_at, 1, 10)"""
            ).fetchall()
        }
        escalation_days = {
            item["day"]: item["count"]
            for item in conn.execute(
                """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS count
                   FROM audit_events WHERE event_type = 'case_escalated'
                   GROUP BY substr(created_at, 1, 10)"""
            ).fetchall()
        }
    today = datetime.now(timezone.utc).date()
    daily = []
    running_total = 0
    running_approved = 0
    running_escalated = 0
    for offset in range(13, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        running_total += case_days.get(day, 0)
        running_approved += approval_days.get(day, 0)
        running_escalated += escalation_days.get(day, 0)
        daily.append(
            {
                "day": day,
                "label": day[8:10],
                "total": running_total,
                "approved": running_approved,
                "escalated": running_escalated,
            }
        )
    approved = row["approved"] or 0
    modified = row["modified"] or 0
    decisions = approved + modified
    return {
        "total": row["total"] or 0,
        "generated": row["generated"] or 0,
        "approved": approved,
        "modified": modified,
        "regenerated": regenerated,
        "escalated": row["escalated"] or 0,
        "approval_rate": round(approved * 100 / decisions, 1) if decisions else 0,
        "avg_confidence": round((row["avg_confidence"] or 0) * 100, 1),
        "avg_generation_ms": round(row["avg_generation_ms"] or 0),
        "avg_resolution_hours": round(row["avg_resolution_hours"] or 0, 1),
        "resolved_within_sla": round((row["resolved_within_sla"] or 0) * 100),
        "feedback": [dict(item) for item in feedback_rows],
        "reasons": [dict(item) for item in reason_rows],
        "daily": daily,
    }


def recent_activity(
    limit: int = 8, path: str | Path | None = None
) -> list[dict]:
    """Ultimi eventi con il contesto minimo della pratica per la dashboard."""
    with session(path) as conn:
        rows = conn.execute(
            """SELECT ae.event_type, ae.created_at, ae.details,
                      rc.id AS case_id, rc.shopify_order_number,
                      rc.customer_name, rc.assigned_operator
               FROM audit_events ae
               JOIN return_cases rc ON rc.id = ae.case_id
               ORDER BY ae.id DESC LIMIT ?""",
            (max(1, min(limit, 50)),),
        ).fetchall()
    activity = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item.get("details") or "{}")
        except json.JSONDecodeError:
            item["details"] = {}
        activity.append(item)
    return activity
