import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.bid_sync_receipts.contract import (
    SCHEMA_VERSION,
    ingest_receipts,
    validate_receipt,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "bid_sync_receipts"


def load_fixture(name):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def complete_receipt():
    return load_fixture("complete_scan.json")


class BidSyncReceiptContractTests(unittest.TestCase):
    def assert_invalid(self, receipt, *expected_codes):
        result = validate_receipt(receipt)
        self.assertFalse(result["valid"])
        codes = {error["code"] for error in result["errors"]}
        for code in expected_codes:
            self.assertIn(code, codes)
        return result

    def test_complete_scan_fixture_is_valid_and_complete(self):
        self.assertEqual(complete_receipt()["schema_version"], SCHEMA_VERSION)
        result = validate_receipt(complete_receipt())
        self.assertTrue(result["valid"], result["errors"])
        self.assertTrue(result["scan_complete"])
        self.assertEqual(result["eligible_updates"], 2)
        self.assertEqual(result["receipt_id"], "bid-sync-4242424242-1")

    def test_partial_page_fixture_is_valid_but_not_complete(self):
        result = validate_receipt(load_fixture("partial_page.json"))
        self.assertTrue(result["valid"], result["errors"])
        self.assertFalse(result["scan_complete"])
        self.assertEqual(result["partial_reason"], "partial_page")

    def test_no_eligible_updates_fixture_is_valid_complete_scan(self):
        result = validate_receipt(load_fixture("no_eligible_updates.json"))
        self.assertTrue(result["valid"], result["errors"])
        self.assertTrue(result["scan_complete"])
        self.assertEqual(result["eligible_updates"], 0)
        self.assertTrue(result["no_eligible_updates"])

    def test_unknown_version_fails_closed(self):
        result = self.assert_invalid(
            load_fixture("unknown_version.json"), "unsupported_schema_version"
        )
        self.assertFalse(result["scan_complete"])

    def test_unknown_version_does_not_inspect_unknown_version_fields(self):
        receipt = load_fixture("unknown_version.json")
        receipt["unexpected_v99_field"] = {"anything": ["goes"]}
        del receipt["scan"]
        result = validate_receipt(receipt)
        self.assertFalse(result["valid"])
        self.assertEqual(
            {error["code"] for error in result["errors"]},
            {"unsupported_schema_version"},
        )

    def test_exact_duplicate_run_id_is_idempotent(self):
        receipt = complete_receipt()
        summary = ingest_receipts([receipt, copy.deepcopy(receipt)])
        self.assertEqual(summary["valid_count"], 1)
        self.assertEqual(summary["exact_duplicate_count"], 1)
        self.assertEqual(summary["conflicting_duplicate_count"], 0)
        self.assertEqual(summary["error_count"], 0)

    def test_conflicting_duplicate_run_id_fails_closed(self):
        first = complete_receipt()
        conflict = copy.deepcopy(first)
        conflict["scan"]["rows_observed"] += 1
        summary = ingest_receipts([first, conflict])
        self.assertEqual(summary["valid_count"], 1)
        self.assertEqual(summary["exact_duplicate_count"], 0)
        self.assertEqual(summary["conflicting_duplicate_count"], 1)
        self.assertIn(
            "conflicting_duplicate_run",
            {error["code"] for error in summary["errors"]},
        )

    def test_same_run_id_with_later_attempt_is_not_a_duplicate(self):
        first = complete_receipt()
        second = copy.deepcopy(first)
        second["run"]["attempt"] = 2
        second["receipt_id"] = "bid-sync-4242424242-2"
        for artifact in second["artifacts"]:
            if artifact["kind"] == "order_book_jsonl_gz":
                artifact["path"] = artifact["path"].replace("-1.jsonl.gz", "-2.jsonl.gz")
        summary = ingest_receipts([first, second])
        self.assertEqual(summary["valid_count"], 2)
        self.assertEqual(summary["conflicting_duplicate_count"], 0)

    def test_complete_invariants_reject_partial_marks(self):
        receipt = complete_receipt()
        receipt["scan"]["terminal_page_complete"] = False
        self.assert_invalid(receipt, "complete_scan_marked_incomplete")

    def test_partial_invariants_require_a_reason_or_incomplete_terminal_page(self):
        receipt = load_fixture("partial_page.json")
        receipt["scan"]["terminal_page_complete"] = True
        receipt["scan"]["incomplete_reason"] = None
        self.assert_invalid(receipt, "partial_scan_missing_evidence")

    def test_no_eligible_updates_flag_must_match_count(self):
        receipt = complete_receipt()
        receipt["updates"]["eligible_updates"] = 0
        self.assert_invalid(receipt, "eligible_update_flag_mismatch")

    def test_eligible_updates_cannot_exceed_candidates(self):
        receipt = complete_receipt()
        receipt["updates"]["eligible_updates"] = receipt["updates"]["candidates_observed"] + 1
        self.assert_invalid(receipt, "eligible_updates_exceed_candidates")

    def test_order_book_path_must_bind_run_id_and_attempt(self):
        receipt = complete_receipt()
        for artifact in receipt["artifacts"]:
            if artifact["kind"] == "order_book_jsonl_gz":
                artifact["path"] = artifact["path"].replace("4242424242-1", "4242424243-1")
        self.assert_invalid(receipt, "artifact_run_mismatch")

    def test_active_bid_paths_are_fixed_snapshot_names(self):
        receipt = complete_receipt()
        for artifact in receipt["artifacts"]:
            if artifact["kind"] == "active_bid_prices_json":
                artifact["path"] = "data/active_bid_prices.preview.json"
        self.assert_invalid(receipt, "artifact_path_mismatch")

    def test_duplicate_artifact_paths_are_rejected(self):
        receipt = complete_receipt()
        artifact = copy.deepcopy(receipt["artifacts"][0])
        artifact["kind"] = "active_bid_prices_json"
        receipt["artifacts"].append(artifact)
        self.assert_invalid(receipt, "duplicate_artifact_path")

    def test_closed_schema_rejects_unexpected_top_level_fields(self):
        receipt = complete_receipt()
        receipt["strategy_note"] = "anonymous"
        self.assert_invalid(receipt, "unexpected_field")

    def test_missing_null_and_wrong_type_required_fields_are_rejected(self):
        receipt = complete_receipt()
        del receipt["run"]["run_id"]
        receipt["scan"]["rows_observed"] = None
        receipt["updates"]["eligible_updates"] = "2"
        result = validate_receipt(receipt)
        self.assertFalse(result["valid"])
        codes = {error["code"] for error in result["errors"]}
        self.assertIn("missing_field", codes)
        self.assertIn("invalid_type", codes)

    def test_negative_huge_boolean_and_nonfinite_numeric_values_are_rejected(self):
        receipt = complete_receipt()
        receipt["run"]["attempt"] = True
        receipt["scan"]["pages_completed"] = -1
        receipt["scan"]["rows_observed"] = 2**53
        receipt["artifacts"][0]["bytes"] = -1
        result = validate_receipt(receipt)
        self.assertFalse(result["valid"])
        codes = {error["code"] for error in result["errors"]}
        self.assertIn("invalid_type", codes)
        self.assertIn("out_of_range", codes)

    def test_zero_and_json_safe_integer_boundaries_are_accepted(self):
        receipt = load_fixture("no_eligible_updates.json")
        receipt["scan"]["pages_completed"] = 0
        receipt["scan"]["rows_observed"] = 0
        receipt["updates"]["candidates_observed"] = 0
        self.assertTrue(validate_receipt(receipt)["valid"])

        receipt = complete_receipt()
        receipt["scan"]["pages_completed"] = 2**53 - 1
        receipt["scan"]["rows_observed"] = 2**53 - 1
        receipt["updates"]["candidates_observed"] = 2**53 - 1
        receipt["updates"]["eligible_updates"] = 2**53 - 1
        receipt["artifacts"][0]["bytes"] = 2**53 - 1
        self.assertTrue(validate_receipt(receipt)["valid"])

    def test_receipt_identity_rejects_empty_unicode_and_mismatched_values(self):
        receipt = complete_receipt()
        receipt["receipt_id"] = "bid-sync-é-1"
        self.assert_invalid(receipt, "invalid_receipt_id")
        receipt = complete_receipt()
        receipt["run"]["run_id"] = ""
        self.assert_invalid(receipt, "invalid_run_id")

    def test_completed_at_must_be_utc_and_not_before_started_at(self):
        receipt = complete_receipt()
        receipt["run"]["completed_at"] = "2026-01-02T03:04:04Z"
        self.assert_invalid(receipt, "invalid_time_order")
        receipt = complete_receipt()
        receipt["run"]["completed_at"] = "2026-01-02T04:07:05+01:00"
        self.assert_invalid(receipt, "timestamp_must_be_utc")

    def test_sha256_must_be_lowercase_hex_and_bytes_must_be_safe_integer(self):
        receipt = complete_receipt()
        receipt["artifacts"][0]["sha256"] = "A" * 64
        receipt["artifacts"][0]["bytes"] = 2**53
        result = validate_receipt(receipt)
        self.assertFalse(result["valid"])
        codes = {error["code"] for error in result["errors"]}
        self.assertIn("invalid_sha256", codes)
        self.assertIn("out_of_range", codes)

    def test_artifacts_may_be_empty_but_must_be_an_array(self):
        receipt = complete_receipt()
        receipt["artifacts"] = []
        self.assertTrue(validate_receipt(receipt)["valid"])
        receipt["artifacts"] = None
        self.assert_invalid(receipt, "invalid_type")

    def test_out_of_order_ingest_still_detects_conflicts(self):
        first = complete_receipt()
        conflict = copy.deepcopy(first)
        conflict["updates"]["candidates_observed"] += 1
        summary = ingest_receipts([conflict, first])
        self.assertEqual(summary["conflicting_duplicate_count"], 1)

    def test_semantic_exact_duplicate_ignores_json_key_order(self):
        first = complete_receipt()
        reordered = {
            "artifacts": first["artifacts"],
            "artifact_versions": first["artifact_versions"],
            "updates": first["updates"],
            "scan": first["scan"],
            "run": first["run"],
            "receipt_id": first["receipt_id"],
            "schema_version": first["schema_version"],
        }
        summary = ingest_receipts([first, reordered])
        self.assertEqual(summary["exact_duplicate_count"], 1)
        self.assertEqual(summary["error_count"], 0)

    def test_unknown_artifact_version_fails_closed(self):
        receipt = complete_receipt()
        receipt["artifact_versions"]["order_book_jsonl_gz"] = "order-book-jsonl/2"
        self.assert_invalid(receipt, "unsupported_artifact_version")

    def test_unknown_artifact_kind_fails_closed(self):
        receipt = complete_receipt()
        receipt["artifacts"] = [dict(receipt["artifacts"][0], kind="private_debug_blob")]
        self.assert_invalid(receipt, "unknown_artifact_kind")

    def test_unhashable_wrong_types_fail_closed_without_crashing(self):
        receipt = complete_receipt()
        receipt["scan"]["status"] = ["complete"]
        self.assert_invalid(receipt, "invalid_scan_status")

        receipt = complete_receipt()
        receipt["scan"]["status"] = {"value": "complete"}
        self.assert_invalid(receipt, "invalid_scan_status")

        receipt = complete_receipt()
        receipt["artifacts"][0]["kind"] = ["order_book_jsonl_gz"]
        self.assert_invalid(receipt, "unknown_artifact_kind")

        receipt = complete_receipt()
        receipt["artifacts"][0]["kind"] = {"value": "order_book_jsonl_gz"}
        self.assert_invalid(receipt, "unknown_artifact_kind")

    def test_huge_digit_run_attempts_fail_closed_without_crashing(self):
        huge_attempt = "9" * 5000

        receipt = complete_receipt()
        receipt["receipt_id"] = f"bid-sync-4242424242-{huge_attempt}"
        self.assert_invalid(receipt, "receipt_id_mismatch")

        receipt = complete_receipt()
        for artifact in receipt["artifacts"]:
            if artifact["kind"] == "order_book_jsonl_gz":
                artifact["path"] = artifact["path"].replace("-1.jsonl.gz", f"-{huge_attempt}.jsonl.gz")
        self.assert_invalid(receipt, "artifact_run_mismatch")

    def test_order_book_date_directory_must_be_a_real_date(self):
        receipt = complete_receipt()
        for artifact in receipt["artifacts"]:
            if artifact["kind"] == "order_book_jsonl_gz":
                artifact["path"] = artifact["path"].replace("2026/01/02", "2026/02/30")
        self.assert_invalid(receipt, "invalid_artifact_date")

    def test_incomplete_reason_allows_unicode_but_rejects_huge_strings(self):
        receipt = load_fixture("partial_page.json")
        receipt["scan"]["incomplete_reason"] = "partial_page_économie_スキャン"
        self.assertTrue(validate_receipt(receipt)["valid"])
        receipt["scan"]["incomplete_reason"] = "x" * 4097
        self.assert_invalid(receipt, "out_of_range")

    def test_incomplete_reason_rejects_all_unicode_control_characters(self):
        for control in ("\x00", "\x1f", "\x7f", "\x85", "\u009f"):
            receipt = load_fixture("partial_page.json")
            receipt["scan"]["incomplete_reason"] = f"partial{control}page"
            self.assert_invalid(receipt, "control_character")

    def test_null_and_nonobject_receipts_fail_closed(self):
        for value in (None, [], "bid-sync-receipt/1", 0):
            self.assert_invalid(value, "invalid_type")

    def test_nonstring_object_keys_fail_closed_without_crashing(self):
        receipt = complete_receipt()
        receipt[1] = "not-json-object-key"
        self.assert_invalid(receipt, "invalid_object_key")

    def test_fixture_output_is_anonymous_metadata_only(self):
        forbidden_keys = {
            "label",
            "threshold_usd",
            "price",
            "market_hash_name",
            "listing_id",
            "account",
            "item_name",
        }
        for fixture in FIXTURE_DIR.glob("*.json"):
            payload = json.loads(fixture.read_text(encoding="utf-8"))

            def walk(value, path=()):
                if isinstance(value, dict):
                    for key, child in value.items():
                        self.assertNotIn(key, forbidden_keys, f"{fixture}:{path + (key,)}")
                        yield from walk(child, path + (key,))
                elif isinstance(value, list):
                    for index, child in enumerate(value):
                        yield from walk(child, path + (str(index),))

            list(walk(payload))
            text = fixture.read_text(encoding="utf-8")
            self.assertNotIn("Sticker", text)
            self.assertNotIn("AK-47", text)
            self.assertNotIn("threshold", text)

    def test_public_contract_document_names_artifact_schemas_and_version_policy(self):
        doc = Path("docs/BID_SYNC_RECEIPTS.md").read_text(encoding="utf-8")
        for required in (
            "bid-sync-receipt/1",
            "data/order_book/YYYY/MM/DD/<run_id>-<attempt>.jsonl.gz",
            "data/active_bid_prices.json",
            "data/active_bid_prices.csv",
            "complete",
            "partial_page",
            "duplicate",
            "unsupported_schema_version",
            "souvenir",
        ):
            self.assertIn(required, doc)


class BidSyncReceiptContractCliTests(unittest.TestCase):
    def run_cli(self, *paths):
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "bid_sync_receipt_contract.py"), *map(str, paths)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_malformed_and_deeply_nested_json_fail_closed_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            malformed = temp_path / "malformed.json"
            malformed.write_text("{bad json", encoding="utf-8")
            depth = 2000
            deep = temp_path / "deep.json"
            deep.write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")

            completed = self.run_cli(malformed)
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertEqual(completed.stderr, "")
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["unreadable_count"], 1)
            self.assertEqual(summary["error_count"], 1)
            self.assertEqual(summary["errors"][0]["code"], "unreadable_receipt")

            completed = self.run_cli(deep)
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertEqual(completed.stderr, "")
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["error_count"], 1)
            self.assertEqual(summary["unreadable_count"] + summary["invalid_count"], 1)
            self.assertIn(summary["errors"][0]["code"], {"unreadable_receipt", "invalid_type"})

    def test_huge_json_integer_tokens_fail_closed_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            huge_integer = Path(temp_dir) / "huge-integer.json"
            huge_integer.write_text('{"value":' + "1" * 5000 + "}", encoding="utf-8")
            completed = self.run_cli(huge_integer)
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertEqual(completed.stderr, "")
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["unreadable_count"], 1)
            self.assertEqual(summary["error_count"], 1)
            self.assertEqual(summary["errors"][0]["code"], "unreadable_receipt")

    def test_duplicate_json_object_keys_fail_closed(self):
        receipt_text = (FIXTURE_DIR / "complete_scan.json").read_text(encoding="utf-8")
        receipt_text = receipt_text.replace(
            '"schema_version": "bid-sync-receipt/1",',
            '"schema_version": "bid-sync-receipt/99",\n  "schema_version": "bid-sync-receipt/1",',
            1,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            duplicate_key = Path(temp_dir) / "duplicate-key.json"
            duplicate_key.write_text(receipt_text, encoding="utf-8")
            completed = self.run_cli(duplicate_key)
            self.assertEqual(completed.returncode, 1, completed.stderr)
            self.assertEqual(completed.stderr, "")
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["unreadable_count"], 1)
            self.assertEqual(summary["errors"][0]["code"], "unreadable_receipt")

    def test_partial_input_failure_preserves_original_indexes_and_valid_receipts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            malformed = Path(temp_dir) / "malformed.json"
            malformed.write_text("{bad json", encoding="utf-8")
            completed = self.run_cli(
                malformed,
                FIXTURE_DIR / "unknown_version.json",
                FIXTURE_DIR / "complete_scan.json",
            )
            self.assertEqual(completed.returncode, 1, completed.stderr)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["valid_count"], 1)
            self.assertEqual(summary["invalid_count"], 1)
            self.assertEqual(summary["unreadable_count"], 1)
            self.assertEqual([error["index"] for error in summary["errors"]], [0, 1])


if __name__ == "__main__":
    unittest.main()
