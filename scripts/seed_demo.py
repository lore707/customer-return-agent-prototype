"""Prepara gli scenari portfolio usando il motore di policy reale."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import database
import demo


def main() -> int:
    database.init_database()
    cases = demo.ensure_showcase()
    print(f"Scenari portfolio disponibili: {len(cases)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
