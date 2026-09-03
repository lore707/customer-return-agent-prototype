"""Caricamento della policy operativa strutturata.

`policies.md` resta il documento leggibile; `return_policies.json` contiene i
valori che il codice può applicare senza interpretazioni generative.
"""

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "return_policies.json"


@lru_cache(maxsize=1)
def load_policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def unresolved_items() -> list[dict]:
    return list(load_policy().get("unresolved", []))


def summary() -> dict:
    policy = load_policy()
    operational_sections = [
        "withdrawal",
        "defective_product",
        "damaged_in_transit",
        "wrong_item",
        "evidence",
        "return_logistics",
        "physical_inspection",
        "escalation",
    ]
    return {
        "version": policy["version"],
        "source": policy["source"],
        "active_sections": len(operational_sections),
        "unresolved": unresolved_items(),
        "unresolved_count": len(unresolved_items()),
    }
