#!/usr/bin/env python3
"""Offline bid-sync receipt contract checker.

Reads one or more receipt JSON files and prints a deterministic compatibility
summary. The command is read-only and uses no network, credentials, account
state, marketplace data, or live artifact contents.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bid_sync_receipts.contract import ingest_receipts


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate anonymous bid-sync receipt JSON files.")
    parser.add_argument("receipts", nargs="+", type=Path, help="receipt JSON files to validate in order")
    args = parser.parse_args()

    receipts = []
    errors = []
    for index, path in enumerate(args.receipts):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            errors.append(
                {
                    "index": index,
                    "code": "unreadable_receipt",
                    "path": str(path),
                    "message": type(exc).__name__,
                }
            )
            continue
        receipts.append((index, receipt))

    summary = ingest_receipts(receipt for _, receipt in receipts)
    remapped_errors = []
    valid_positions = [index for index, _ in receipts]
    for error in summary["errors"]:
        error = dict(error)
        ingest_index = error.get("index")
        if isinstance(ingest_index, int) and 0 <= ingest_index < len(valid_positions):
            error["index"] = valid_positions[ingest_index]
        remapped_errors.append(error)

    summary["errors"] = errors + remapped_errors
    summary["error_count"] = len(summary["errors"])
    summary["unreadable_count"] = len(errors)
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 1 if summary["error_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
