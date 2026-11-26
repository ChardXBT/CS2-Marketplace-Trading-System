#!/usr/bin/env python3
"""Offline operator dry-run for rejection-receipt recreation holds.

Reads only local JSON/JSONL fixtures and prints the disabled eligibility
decision as JSON.  It never writes the ledger and never contacts a marketplace.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rejection_receipts.cooldown import (  # noqa: E402
    ReceiptConflictError,
    ReceiptStore,
    ReceiptStoreCorruptionError,
    ReceiptValidationError,
    TargetIdentity,
    evaluate_recreation,
    parse_instant,
)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--ledger", required=True, help="local receipt JSONL ledger to read (never written)")
    result.add_argument("--receipt-json", action="append", default=[], help="local receipt JSON file to overlay in memory")
    result.add_argument("--marketplace", required=True)
    result.add_argument("--account", required=True)
    result.add_argument("--target-key", required=True)
    result.add_argument("--expression-sha256", required=True)
    result.add_argument("--at", help="RFC3339 evaluation time; defaults to the current UTC time")
    return result


def receipt_files(paths):
    rows = []
    for name in paths:
        try:
            payload = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReceiptValidationError(f"invalid_receipt_file:{name}") from exc
        if isinstance(payload, list):
            rows.extend(payload)
        else:
            rows.append(payload)
    return rows


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        checked_at = parse_instant(args.at, "at") if args.at else datetime.now(timezone.utc)
        identity = TargetIdentity(
            marketplace=args.marketplace,
            account=args.account,
            target_key=args.target_key,
            expression_sha256=args.expression_sha256,
        )
        # The evaluation instant is the trust boundary for this dry run: local
        # fixtures dated after it are rejected by the same clock-skew guard.
        store = ReceiptStore(args.ledger, now=lambda: checked_at)
        receipts = [*store.load(), *receipt_files(args.receipt_json)]
        decision = evaluate_recreation(identity, receipts, checked_at)
    except (ReceiptValidationError, ReceiptConflictError, ReceiptStoreCorruptionError, ValueError) as exc:
        reason = getattr(exc, "reason", str(exc))
        print(json.dumps({"ok": False, "error": reason}, sort_keys=True), file=sys.stderr)
        return 2

    output = {"ok": True, **decision.to_dict()}
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
