"""Load a small, representative warehouse for the end-to-end API tests.

The API tests assert against a populated warehouse - rankings have a leader,
an airport has a trend, airlines have an aggregate to exclude. CI applies
the migrations and then runs them against an empty database, so eight tests
asserted on data that was never loaded and had failed on every run since the
workflow was added.

Skipping them instead would be worse: they cover the like-for-like ranking
guard and the airport-code validation, both of which exist because of real
defects, and a skipped test protects nothing.

    python -m tests.seed_warehouse
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "warehouse_seed.jsonl"


def main() -> int:
    from services.warehouse.loader import WarehouseLoader

    if not FIXTURE.exists():
        print(f"no fixture at {FIXTURE}")
        return 1

    # The loader reads a facts file and an optional ledger; the fixture
    # carries its own provenance, so no ledger is needed.
    summary = WarehouseLoader().load(FIXTURE, None)
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("loaded") else 1


if __name__ == "__main__":
    raise SystemExit(main())
