"""SQLite repository for the generic workspace onboarding model."""

from __future__ import annotations

import json
import uuid

import database


JSON_FIELDS = {
    "workspaces": ("markets", "derived_context"),
    "operations": ("operational_model",),
    "knowledge_sources": ("metadata",),
    "clarifications": ("options", "details"),
}


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _row(row, kind: str) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    for field in JSON_FIELDS.get(kind, ()):
        try:
            item[field] = json.loads(item.get(field) or ("[]" if field in {"markets", "options"} else "{}"))
        except (TypeError, json.JSONDecodeError):
            item[field] = [] if field in {"markets", "options"} else {}
    return item


def create_workspace(path=None) -> dict:
    workspace_id = _id("WS")
    now = database.utc_now()
    with database.session(path) as conn:
        conn.execute(
            """INSERT INTO workspaces
               (id, status, current_step, completeness, created_at, updated_at)
               VALUES (?, 'draft', 0, 0, ?, ?)""",
            (workspace_id, now, now),
        )
    return get_workspace(workspace_id, path)


def get_workspace(workspace_id: str | None, path=None) -> dict | None:
    if not workspace_id:
        return None
    with database.session(path) as conn:
        row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    return _row(row, "workspaces")


def latest_completed_workspace(path=None) -> dict | None:
    with database.session(path) as conn:
        row = conn.execute(
            "SELECT * FROM workspaces WHERE status = 'completed' ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
    return _row(row, "workspaces")


def update_workspace(workspace_id: str, values: dict, *, path=None) -> dict:
    allowed = {
        "company_name", "company_description", "industry", "markets",
        "business_model", "team_size", "derived_context", "status",
        "current_step", "completeness", "active_operation_id", "completed_at",
    }
    clean = {key: value for key, value in values.items() if key in allowed}
    if not clean:
        return get_workspace(workspace_id, path)
    for field in ("markets", "derived_context"):
        if field in clean:
            clean[field] = json.dumps(clean[field], ensure_ascii=False)
    clean["updated_at"] = database.utc_now()
    assignments = ", ".join(f"{key} = ?" for key in clean)
    with database.session(path) as conn:
        changed = conn.execute(
            f"UPDATE workspaces SET {assignments} WHERE id = ?",
            [*clean.values(), workspace_id],
        ).rowcount
    if not changed:
        raise KeyError(workspace_id)
    return get_workspace(workspace_id, path)


def save_company(workspace_id: str, values: dict, *, path=None) -> dict:
    description = str(values.get("company_description") or "").strip()
    company_name = str(values.get("company_name") or "").strip()
    if len(company_name) < 2 or len(description) < 20:
        raise ValueError("Add a company name and a short description of at least 20 characters.")
    markets = values.get("markets") or []
    if isinstance(markets, str):
        markets = [item.strip() for item in re_split_markets(markets) if item.strip()]
    derived = {
        "summary": description[:240],
        "operating_scope": " / ".join(markets[:4]) if markets else "To be refined",
        "model": str(values.get("business_model") or "Not specified"),
    }
    return update_workspace(
        workspace_id,
        {
            "company_name": company_name[:160],
            "company_description": description[:8_000],
            "industry": str(values.get("industry") or "").strip()[:120],
            "markets": markets[:12],
            "business_model": str(values.get("business_model") or "").strip()[:40],
            "team_size": str(values.get("team_size") or "").strip()[:40],
            "derived_context": derived,
            "current_step": 2,
            "completeness": max(18, int(get_workspace(workspace_id, path).get("completeness") or 0)),
        },
        path=path,
    )


def re_split_markets(value: str) -> list[str]:
    return value.replace(";", ",").split(",")


def save_operation(workspace_id: str, values: dict, *, path=None) -> dict:
    description = str(values.get("description") or "").strip()
    objective = str(values.get("objective") or "").strip()
    if len(description) < 20 or len(objective) < 12:
        raise ValueError("Describe the operation and its objective in a little more detail.")
    workspace = get_workspace(workspace_id, path)
    if not workspace:
        raise KeyError(workspace_id)
    operation_id = workspace.get("active_operation_id") or _id("OP")
    now = database.utc_now()
    with database.session(path) as conn:
        existing = conn.execute("SELECT id FROM operations WHERE id = ?", (operation_id,)).fetchone()
        if existing:
            conn.execute(
                """UPDATE operations SET name = ?, description = ?, objective = ?,
                   current_process = ?, updated_at = ? WHERE id = ?""",
                (
                    str(values.get("name") or "").strip()[:160] or None,
                    description[:12_000], objective[:6_000],
                    str(values.get("current_process") or "").strip()[:12_000] or None,
                    now, operation_id,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO operations
                   (id, workspace_id, name, description, objective, current_process,
                    status, operational_model, completeness, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'draft', '{}', 0, ?, ?)""",
                (
                    operation_id, workspace_id,
                    str(values.get("name") or "").strip()[:160] or None,
                    description[:12_000], objective[:6_000],
                    str(values.get("current_process") or "").strip()[:12_000] or None,
                    now, now,
                ),
            )
    update_workspace(
        workspace_id,
        {"active_operation_id": operation_id, "current_step": 3, "completeness": max(32, int(workspace.get("completeness") or 0))},
        path=path,
    )
    return get_operation(operation_id, path)


def get_operation(operation_id: str | None, path=None) -> dict | None:
    if not operation_id:
        return None
    with database.session(path) as conn:
        row = conn.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
    return _row(row, "operations")


def active_operation(workspace_id: str | None, path=None) -> dict | None:
    workspace = get_workspace(workspace_id, path)
    return get_operation(workspace.get("active_operation_id"), path) if workspace else None


def list_operations(workspace_id: str | None = None, path=None) -> list[dict]:
    with database.session(path) as conn:
        if workspace_id:
            rows = conn.execute(
                "SELECT * FROM operations WHERE workspace_id = ? ORDER BY updated_at DESC", (workspace_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM operations ORDER BY updated_at DESC").fetchall()
    return [_row(row, "operations") for row in rows]


def add_knowledge_source(
    workspace_id: str,
    operation_id: str,
    *,
    name: str,
    source_type: str,
    content: str,
    metadata: dict | None = None,
    path=None,
) -> dict:
    if len(content.strip()) < 20:
        raise ValueError("This source does not contain enough usable text.")
    source_id = _id("SRC")
    now = database.utc_now()
    with database.session(path) as conn:
        conn.execute(
            """INSERT INTO knowledge_sources
               (id, workspace_id, operation_id, name, source_type, content,
                status, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?)""",
            (
                source_id, workspace_id, operation_id, name[:180], source_type[:40],
                content[:100_000], json.dumps(metadata or {}, ensure_ascii=False), now,
            ),
        )
    update_workspace(workspace_id, {"current_step": 4, "completeness": 44}, path=path)
    return get_knowledge_source(source_id, path)


def get_knowledge_source(source_id: str, path=None) -> dict | None:
    with database.session(path) as conn:
        row = conn.execute("SELECT * FROM knowledge_sources WHERE id = ?", (source_id,)).fetchone()
    return _row(row, "knowledge_sources")


def list_knowledge_sources(operation_id: str, path=None) -> list[dict]:
    with database.session(path) as conn:
        rows = conn.execute(
            "SELECT * FROM knowledge_sources WHERE operation_id = ? ORDER BY created_at", (operation_id,)
        ).fetchall()
    return [_row(row, "knowledge_sources") for row in rows]


def remove_knowledge_source(source_id: str, workspace_id: str, *, path=None) -> bool:
    with database.session(path) as conn:
        changed = conn.execute(
            "DELETE FROM knowledge_sources WHERE id = ? AND workspace_id = ?", (source_id, workspace_id)
        ).rowcount
    return bool(changed)


def save_generated_model(workspace_id: str, operation_id: str, result: dict, *, path=None) -> dict:
    model = result["model"]
    now = database.utc_now()
    with database.session(path) as conn:
        conn.execute(
            """UPDATE operations SET name = ?, operational_model = ?, completeness = ?,
               status = 'review', updated_at = ? WHERE id = ? AND workspace_id = ?""",
            (
                model["operation"]["name"], json.dumps(model, ensure_ascii=False),
                int(model.get("completeness") or 0), now, operation_id, workspace_id,
            ),
        )
        conn.execute("DELETE FROM clarifications WHERE operation_id = ?", (operation_id,))
        for item in result.get("clarifications") or []:
            conn.execute(
                """INSERT INTO clarifications
                   (id, operation_id, issue_type, question, options, status,
                    details, created_at) VALUES (?, ?, ?, ?, ?, 'open', ?, ?)""",
                (
                    _id("CLR"), operation_id, item["issue_type"], item["question"],
                    json.dumps(item.get("options") or [], ensure_ascii=False),
                    json.dumps(item.get("details") or {}, ensure_ascii=False), now,
                ),
            )
    update_workspace(
        workspace_id,
        {"current_step": 5 if result.get("clarifications") else 6, "completeness": int(model.get("completeness") or 0)},
        path=path,
    )
    return get_operation(operation_id, path)


def list_clarifications(operation_id: str, path=None) -> list[dict]:
    with database.session(path) as conn:
        rows = conn.execute(
            "SELECT * FROM clarifications WHERE operation_id = ? ORDER BY created_at, id", (operation_id,)
        ).fetchall()
    return [_row(row, "clarifications") for row in rows]


def resolve_clarifications(workspace_id: str, operation_id: str, answers: dict, updated_model: dict, *, path=None) -> None:
    now = database.utc_now()
    with database.session(path) as conn:
        for clarification_id, answer in answers.items():
            conn.execute(
                """UPDATE clarifications SET answer = ?, status = 'resolved', resolved_at = ?
                   WHERE id = ? AND operation_id = ?""",
                (str(answer)[:500], now, clarification_id, operation_id),
            )
        conn.execute(
            """UPDATE operations SET operational_model = ?, completeness = ?, updated_at = ?
               WHERE id = ?""",
            (json.dumps(updated_model, ensure_ascii=False), int(updated_model.get("completeness") or 0), now, operation_id),
        )
    update_workspace(
        workspace_id,
        {"current_step": 6, "completeness": int(updated_model.get("completeness") or 0)},
        path=path,
    )


def replace_test_scenarios(workspace_id: str, operation_id: str, scenarios: list[dict], *, path=None) -> list[dict]:
    now = database.utc_now()
    with database.session(path) as conn:
        conn.execute("DELETE FROM test_scenarios WHERE operation_id = ?", (operation_id,))
        for item in scenarios:
            conn.execute(
                """INSERT INTO test_scenarios
                   (id, operation_id, title, input_summary, recommendation, rationale,
                    status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    _id("TST"), operation_id, item["title"], item["input_summary"],
                    item["recommendation"], item["rationale"], now, now,
                ),
            )
    update_workspace(workspace_id, {"current_step": 7}, path=path)
    return list_test_scenarios(operation_id, path)


def list_test_scenarios(operation_id: str, path=None) -> list[dict]:
    with database.session(path) as conn:
        rows = conn.execute(
            "SELECT * FROM test_scenarios WHERE operation_id = ? ORDER BY created_at, id", (operation_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def save_scenario_feedback(workspace_id: str, operation_id: str, feedback: list[dict], *, path=None) -> list[dict]:
    now = database.utc_now()
    with database.session(path) as conn:
        for item in feedback:
            status = item.get("status") if item.get("status") in {"correct", "adjustment"} else "pending"
            conn.execute(
                """UPDATE test_scenarios SET status = ?, feedback = ?, updated_at = ?
                   WHERE id = ? AND operation_id = ?""",
                (status, str(item.get("feedback") or "")[:1_000] or None, now, item.get("id"), operation_id),
            )
    update_workspace(workspace_id, {"current_step": 8}, path=path)
    return list_test_scenarios(operation_id, path)


def complete_workspace(workspace_id: str, *, path=None) -> dict:
    workspace = get_workspace(workspace_id, path)
    operation = active_operation(workspace_id, path)
    if not workspace or not operation or not operation.get("operational_model"):
        raise ValueError("Complete the operational model before entering the workspace.")
    completeness = max(int(workspace.get("completeness") or 0), int(operation.get("completeness") or 0))
    update_workspace(
        workspace_id,
        {
            "status": "completed", "current_step": 8, "completeness": completeness,
            "completed_at": database.utc_now(),
        },
        path=path,
    )
    with database.session(path) as conn:
        conn.execute("UPDATE operations SET status = 'active', updated_at = ? WHERE id = ?", (database.utc_now(), operation["id"]))
    return get_workspace(workspace_id, path)


def onboarding_state(workspace_id: str | None, path=None) -> dict:
    workspace = get_workspace(workspace_id, path)
    if not workspace:
        return {"workspace": None, "operation": None, "sources": [], "clarifications": [], "scenarios": []}
    operation = active_operation(workspace_id, path)
    return {
        "workspace": workspace,
        "operation": operation,
        "sources": list_knowledge_sources(operation["id"], path) if operation else [],
        "clarifications": list_clarifications(operation["id"], path) if operation else [],
        "scenarios": list_test_scenarios(operation["id"], path) if operation else [],
    }
