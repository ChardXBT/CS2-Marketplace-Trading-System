import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
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
    holds_from_receipts,
    validate_receipt,
)

FIXTURES = ROOT / "tests" / "fixtures" / "rejection_receipts"
FIXED_NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)
EXPRESSION_A = "a" * 64
EXPRESSION_B = "b" * 64


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def exact_target(**changes):
    values = {
        "marketplace": "csfloat",
        "account": "anonymous-account",
        "target_key": "anonymous-target-a",
        "expression_sha256": EXPRESSION_A,
    }
    values.update(changes)
    return TargetIdentity(**values)


class RejectionReceiptBoundaryTests(unittest.TestCase):
    def test_confirmed_receipt_blocks_only_the_exact_target_while_disabled(self):
        receipt = fixture("active_confirmed.json")
        validate_receipt(receipt, now=FIXED_NOW)

        decision = evaluate_recreation(exact_target(), [receipt], FIXED_NOW)

        self.assertEqual(decision.decision, "hold")
        self.assertFalse(decision.applied)
        self.assertEqual(decision.consumer_state, "disabled")
        self.assertEqual(decision.receipt_ids, (receipt["receipt_id"],))
        self.assertEqual(decision.reasons, ("missing_sticker",))
        self.assertEqual(decision.expires_at, datetime(2026, 11, 29, tzinfo=timezone.utc))

        unrelated = [
            exact_target(marketplace="other-market"),
            exact_target(account="other-anonymous-account"),
            exact_target(target_key="anonymous-target-b"),
            exact_target(expression_sha256=EXPRESSION_B),
        ]
        for target in unrelated:
            with self.subTest(target=target):
                self.assertEqual(evaluate_recreation(target, [receipt], FIXED_NOW).decision, "allow")

    def test_expiry_boundary_is_exact(self):
        receipt = fixture("expired_confirmed.json")
        expiry = datetime(2020, 1, 4, tzinfo=timezone.utc)

        before = evaluate_recreation(exact_target(), [receipt], expiry - timedelta(microseconds=1))
        at = evaluate_recreation(exact_target(), [receipt], expiry)
        after = evaluate_recreation(exact_target(), [receipt], expiry + timedelta(microseconds=1))

        self.assertEqual(before.decision, "hold")
        self.assertEqual(at.decision, "allow")
        self.assertEqual(after.decision, "allow")
        self.assertEqual(at.receipt_ids, ())

    def test_in_memory_repeat_receipts_are_deduplicated(self):
        receipt = fixture("active_confirmed.json")
        duplicate = fixture("duplicate_confirmed.json")

        decision = evaluate_recreation(exact_target(), [receipt, duplicate], FIXED_NOW)

        self.assertEqual(decision.decision, "hold")
        self.assertEqual(decision.receipt_ids, (receipt["receipt_id"],))

    def test_primary_producer_receipts_are_schema_compatible(self):
        confirmed = fixture("producer_confirmed_missing_sticker.json")
        manual = fixture("producer_manual_review_unknown.json")
        validate_receipt(confirmed, now=FIXED_NOW)
        validate_receipt(manual, now=FIXED_NOW)

        producer_target = exact_target()
        decision = evaluate_recreation(producer_target, [confirmed], FIXED_NOW)
        self.assertEqual(decision.decision, "hold")
        self.assertEqual(decision.receipt_ids, (confirmed["receipt_id"],))
        self.assertEqual(decision.expires_at, datetime(2026, 11, 29, 12, tzinfo=timezone.utc))
        self.assertEqual(evaluate_recreation(producer_target, [manual], FIXED_NOW).decision, "allow")

    def test_confirmed_mismatch_without_proven_target_is_evidence_not_a_hold(self):
        receipt = fixture("producer_confirmed_missing_sticker.json")
        receipt["order_target"] = None
        receipt["hold"] = None

        validate_receipt(receipt, now=FIXED_NOW)
        self.assertEqual(holds_from_receipts([receipt], now=FIXED_NOW), ())
        self.assertEqual(evaluate_recreation(exact_target(), [receipt], FIXED_NOW).decision, "allow")

    def test_manual_review_missing_inspect_data_never_creates_a_hold(self):
        receipt = fixture("manual_review.json")
        validate_receipt(receipt, now=FIXED_NOW)

        self.assertEqual(holds_from_receipts([receipt], now=FIXED_NOW), ())
        self.assertEqual(evaluate_recreation(exact_target(), [receipt], FIXED_NOW).decision, "allow")

    def test_duplicate_receipt_is_idempotent_and_conflicting_duplicate_fails_closed(self):
        first = fixture("active_confirmed.json")
        duplicate = fixture("duplicate_confirmed.json")
        conflict = copy.deepcopy(duplicate)
        conflict["asset_id"] = "9100000000000000999"

        with tempfile.TemporaryDirectory() as tmp:
            store = ReceiptStore(Path(tmp) / "receipts.jsonl", now=lambda: FIXED_NOW)
            initial = store.append(first)
            repeated = store.append(duplicate)

            self.assertTrue(initial.written)
            self.assertFalse(repeated.written)
            self.assertTrue(repeated.duplicate)
            self.assertEqual(len((store.path).read_text(encoding="utf-8").splitlines()), 1)
            self.assertEqual(len(store.load()), 1)

            with self.assertRaises(ReceiptConflictError) as raised:
                store.append(conflict)
            self.assertEqual(raised.exception.reason, "conflicting_duplicate_receipt")
            self.assertEqual(len((store.path).read_text(encoding="utf-8").splitlines()), 1)

    def test_restart_reloads_durable_identity_without_extending_or_duplicating_hold(self):
        receipt = fixture("active_confirmed.json")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.jsonl"
            first_store = ReceiptStore(path, now=lambda: FIXED_NOW)
            first_store.append(receipt)
            first_decision = evaluate_recreation(exact_target(), first_store.load(), FIXED_NOW)

            restarted_store = ReceiptStore(path, now=lambda: FIXED_NOW)
            restarted_receipts = restarted_store.load()
            restarted_decision = evaluate_recreation(exact_target(), restarted_receipts, FIXED_NOW)

            self.assertEqual(len(restarted_receipts), 1)
            self.assertEqual(first_decision.to_dict(), restarted_decision.to_dict())
            self.assertEqual(restarted_decision.expires_at, datetime(2026, 11, 29, tzinfo=timezone.utc))

    def test_validation_failure_ids_are_stable_for_bad_receipts(self):
        cases = []

        def case(reason, mutate):
            receipt = fixture("active_confirmed.json")
            mutate(receipt)
            cases.append((reason, receipt))

        case("unsupported_schema", lambda r: r.update(schema="other/1"))
        case("invalid_receipt_id", lambda r: r.update(receipt_id=""))
        case("invalid_detected_at", lambda r: r.update(detected_at="soon"))
        case("future_receipt", lambda r: (
            r.update(detected_at="2026-11-27T01:00:01Z"),
            r["hold"].update(expires_at="2026-11-30T01:00:01Z")
        ))
        case("invalid_verdict", lambda r: r.update(verdict="maybe"))
        case("invalid_mismatch", lambda r: r.update(mismatch="scratch"))
        case("mismatch_required", lambda r: r.update(mismatch="none"))
        case("incomplete_order_target", lambda r: r["order_target"].update(target_key=None))
        case("invalid_expression_sha256", lambda r: r["order_target"].update(expression_sha256="x"))
        case("invalid_hold_type", lambda r: r["hold"].update(type="cancel_order"))
        case("invalid_hold_duration", lambda r: r["hold"].update(duration_hours=71))
        case("invalid_hold_scope", lambda r: r["hold"].update(scope="account_wide"))
        case("invalid_hold_state", lambda r: r["hold"].update(state="active"))
        case("hold_expiry_mismatch", lambda r: r["hold"].update(expires_at="2026-11-29T00:00:01Z"))
        case("invalid_reason", lambda r: r.update(reason="NotControlled"))

        for reason, receipt in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(ReceiptValidationError) as raised:
                    validate_receipt(receipt, now=FIXED_NOW)
                self.assertEqual(raised.exception.reason, reason)

    def test_closed_schema_rejects_unexpected_fields_and_cross_account_identity(self):
        cases = []

        def case(reason, mutate):
            receipt = fixture("active_confirmed.json")
            mutate(receipt)
            cases.append((reason, receipt))

        case("unexpected_receipt_field", lambda r: r.update(cookie="anonymous-secret-shaped-value"))
        case("unexpected_order_target_field", lambda r: r["order_target"].update(notes="uncontrolled"))
        case("unexpected_hold_field", lambda r: r["hold"].update(action="cancel"))
        case("unexpected_evidence_field", lambda r: r["actual_evidence"].update(raw="uncontrolled"))
        case("order_target_account_mismatch", lambda r: r["order_target"].update(account="other-anonymous-account"))
        case("order_target_marketplace_mismatch", lambda r: r["order_target"].update(marketplace="other-market"))

        for reason, receipt in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(ReceiptValidationError) as raised:
                    validate_receipt(receipt, now=FIXED_NOW)
                self.assertEqual(raised.exception.reason, reason)

    def test_manual_review_with_a_hold_is_rejected(self):
        receipt = fixture("manual_review.json")
        receipt["hold"] = fixture("active_confirmed.json")["hold"]

        with self.assertRaises(ReceiptValidationError) as raised:
            validate_receipt(receipt, now=FIXED_NOW)
        self.assertEqual(raised.exception.reason, "manual_review_hold_forbidden")

    def test_unicode_identity_is_matched_exactly(self):
        receipt = fixture("active_confirmed.json")
        receipt["account"] = "anonymous-ü-account"
        receipt["order_target"]["account"] = "anonymous-ü-account"
        receipt["order_target"]["target_key"] = "-target-🎯"
        validate_receipt(receipt, now=FIXED_NOW)

        matching = exact_target(account="anonymous-ü-account", target_key="-target-🎯")
        different = exact_target(account="anonymous-ü-account", target_key="-target-🎯 ")
        self.assertEqual(evaluate_recreation(matching, [receipt], FIXED_NOW).decision, "hold")
        self.assertEqual(evaluate_recreation(different, [receipt], FIXED_NOW).decision, "allow")

    def test_clock_skew_boundary_and_timezone_offsets(self):
        def instant(value):
            return value.isoformat().replace("+00:00", "Z")

        receipt = fixture("active_confirmed.json")
        receipt["detected_at"] = "2026-11-26T02:30:00+02:30"
        receipt["hold"]["expires_at"] = "2026-11-29T02:30:00+02:30"
        validate_receipt(receipt, now=FIXED_NOW)
        self.assertEqual(
            evaluate_recreation(exact_target(), [receipt], FIXED_NOW).expires_at,
            datetime(2026, 11, 29, tzinfo=timezone.utc),
        )

        for offset, expected in [(timedelta(seconds=120), True), (timedelta(seconds=121), False)]:
            future = fixture("active_confirmed.json")
            detected = FIXED_NOW + offset
            future["detected_at"] = instant(detected)
            future["hold"]["expires_at"] = instant(detected + timedelta(hours=72))
            with self.subTest(offset=offset.total_seconds()):
                if expected:
                    validate_receipt(future, now=FIXED_NOW)
                else:
                    with self.assertRaises(ReceiptValidationError) as raised:
                        validate_receipt(future, now=FIXED_NOW)
                    self.assertEqual(raised.exception.reason, "future_receipt")

    def test_zero_negative_empty_none_and_huge_edge_values_fail_closed(self):
        for duration in (0, -1, False, 720000000000):
            receipt = fixture("active_confirmed.json")
            receipt["hold"]["duration_hours"] = duration
            with self.subTest(duration=duration):
                with self.assertRaises(ReceiptValidationError) as raised:
                    validate_receipt(receipt, now=FIXED_NOW)
                self.assertEqual(raised.exception.reason, "invalid_hold_duration")

        receipt = fixture("active_confirmed.json")
        receipt["order_target"] = None
        with self.assertRaises(ReceiptValidationError) as raised:
            validate_receipt(receipt, now=FIXED_NOW)
        self.assertEqual(raised.exception.reason, "incomplete_order_target")

        receipt = fixture("active_confirmed.json")
        receipt["receipt_id"] = "x" * 129
        with self.assertRaises(ReceiptValidationError) as raised:
            validate_receipt(receipt, now=FIXED_NOW)
        self.assertEqual(raised.exception.reason, "invalid_receipt_id")

    def test_out_of_order_receipt_replay_is_deterministic(self):
        active = fixture("active_confirmed.json")
        expired = fixture("expired_confirmed.json")
        orders = [
            evaluate_recreation(exact_target(), [expired, active], FIXED_NOW).to_dict(),
            evaluate_recreation(exact_target(), [active, expired], FIXED_NOW).to_dict(),
        ]
        self.assertEqual(orders[0], orders[1])
        self.assertEqual(orders[0]["decision"], "hold")
        self.assertEqual(orders[0]["receipt_ids"], [active["receipt_id"]])

    def test_cooperating_concurrent_appends_keep_every_distinct_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ReceiptStore(Path(tmp) / "receipts.jsonl", now=lambda: FIXED_NOW)
            errors = []

            def append_copy(index):
                try:
                    receipt = fixture("active_confirmed.json")
                    receipt["receipt_id"] = f"{index + 10:064d}"
                    receipt["asset_id"] = f"9100000000000000{index:03d}"
                    store.append(receipt)
                except Exception as exc:  # pragma: no cover - failure is asserted below.
                    errors.append(exc)

            threads = [threading.Thread(target=append_copy, args=(index,)) for index in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(errors, [])
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(len(store.load()), 5)
            self.assertEqual(len((store.path).read_text(encoding="utf-8").splitlines()), 5)

    def test_corrupt_or_nonobject_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.jsonl"
            path.write_text('{"schema":\n', encoding="utf-8")
            store = ReceiptStore(path, now=lambda: FIXED_NOW)
            with self.assertRaises(ReceiptStoreCorruptionError) as raised:
                store.load()
            self.assertEqual(raised.exception.reason, "corrupt_ledger_line_1")

            path.write_text('[]\n', encoding="utf-8")
            with self.assertRaises(ReceiptStoreCorruptionError) as raised:
                store.load()
            self.assertEqual(raised.exception.reason, "corrupt_ledger_line_1")

    def test_operator_dry_run_reads_fixtures_without_writing_or_mutating(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "receipts.jsonl"
            command = [
                sys.executable,
                str(ROOT / "tools" / "rejection_receipt_dry_run.py"),
                "--ledger", str(ledger),
                "--receipt-json", str(FIXTURES / "active_confirmed.json"),
                "--marketplace", "csfloat",
                "--account", "anonymous-account",
                "--target-key", "anonymous-target-a",
                "--expression-sha256", EXPRESSION_A,
                "--at", "2026-11-27T00:00:00Z",
            ]
            result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["decision"], "hold")
            self.assertFalse(output["applied"])
            self.assertEqual(output["consumer_state"], "disabled")
            self.assertFalse(ledger.exists(), "dry-run must not create or modify the durable ledger")


if __name__ == "__main__":
    unittest.main()
