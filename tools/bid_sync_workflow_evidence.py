#!/usr/bin/env python3
"""Offline GitHub Actions evidence checker for bid-sync runs.

Reads already-retrieved run, jobs, and receipt JSON files. The command is
read-only and uses no network, credentials, account state, marketplace data,
or live artifact contents.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bid_sync_receipts.contract import loads_receipt_json
from src.bid_sync_receipts.workflow_evidence import validate_workflow_sync_evidence


def _load_json(path: Path):
    try:
        return loads_receipt_json(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError, RecursionError) as exc:
        return None, {
            "code": "unreadable_evidence",
            "path": str(path),
            "message": type(exc).__name__,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate archived Actions evidence for a bid-sync run.")
    parser.add_argument("run", type=Path, help="GitHub Actions workflow-run JSON file")
    parser.add_argument("jobs", type=Path, help="GitHub Actions jobs response JSON file")
    parser.add_argument("receipt", type=Path, help="bid-sync receipt JSON file")
    args = parser.parse_args()

    run, run_error = _load_json(args.run)
    jobs, jobs_error = _load_json(args.jobs)
    receipt, receipt_error = _load_json(args.receipt)
    load_errors = [error for error in (run_error, jobs_error, receipt_error) if error is not None]
    if load_errors:
        result = {
            "valid": False,
            "errors": load_errors,
            "run_id": None,
            "attempt": None,
            "artifact_kinds": [],
        }
    else:
        result = validate_workflow_sync_evidence(run, jobs, receipt)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
