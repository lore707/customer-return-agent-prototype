"""Context minimisation before an operational model service is called.

Keeping this layer separate makes the external-provider boundary explicit and
gives redaction, minimisation, consent and retention controls one clear home.
"""

from __future__ import annotations

import re


MAX_SOURCE_CHARACTERS = 24_000


def _redact(value: str) -> tuple[str, int]:
    text = str(value or "").strip()
    replacements = 0
    patterns = (
        (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL]"),
        (r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\d[\s.-]?){8,12}(?!\w)", "[PHONE]"),
        (r"(?i)\b(?:password|api[ _-]?key|secret|token)\s*[:=]\s*\S+", "[SECRET REMOVED]"),
    )
    for pattern, replacement in patterns:
        text, count = re.subn(pattern, replacement, text, flags=re.IGNORECASE)
        replacements += count
    return text, replacements


def prepare_operational_context(
    workspace: dict,
    operation: dict,
    sources: list[dict],
) -> dict:
    """Return the smallest useful, redacted payload for model generation."""

    redactions = 0

    def clean(value: str, limit: int = 8_000) -> str:
        nonlocal redactions
        cleaned, count = _redact(value)
        redactions += count
        return cleaned[:limit]

    prepared_sources = []
    remaining = MAX_SOURCE_CHARACTERS
    for source in sources:
        if remaining <= 0:
            break
        content = clean(source.get("content") or "", remaining)
        remaining -= len(content)
        prepared_sources.append(
            {
                "id": source.get("id"),
                "name": clean(source.get("name") or "Knowledge source", 160),
                "source_type": source.get("source_type") or "text",
                "content": content,
            }
        )

    derived = workspace.get("derived_context") if isinstance(workspace.get("derived_context"), dict) else {}
    return {
        "company": {
            "name": clean(workspace.get("company_name") or "", 160),
            "description": clean(workspace.get("company_description") or ""),
            "industry": clean(workspace.get("industry") or "", 120),
            "markets": workspace.get("markets") or [],
            "business_model": workspace.get("business_model") or "",
            "team_size": workspace.get("team_size") or "",
            "core_business": clean(derived.get("core_business") or workspace.get("company_description") or ""),
            "operational_activities": clean(derived.get("operational_activities") or operation.get("description") or "", 12_000),
            "operational_challenges": clean(derived.get("operational_challenges") or ""),
        },
        "operation": {
            "name": clean(operation.get("name") or "", 160),
            "description": clean(operation.get("description") or ""),
            "objective": clean(operation.get("objective") or ""),
            "current_process": clean(operation.get("current_process") or ""),
        },
        "knowledge_sources": prepared_sources,
        "privacy": {
            "redactions": redactions,
            "source_characters_used": MAX_SOURCE_CHARACTERS - remaining,
            "external_provider_used": False,
        },
    }
