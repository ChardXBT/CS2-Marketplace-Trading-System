"""Validate the public, anonymous bid-sync receipt contract.

The validator is deliberately offline and pure. It validates receipt metadata
only; it never reads live artifact contents or marketplace data.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from typing import Any, Iterable

SCHEMA_VERSION = "bid-sync-receipt/1"
REPOSITORY = "ChardXBT/CS2-Marketplace-Trading-System"
WORKFLOW = "CSFloat Hourly Monitor"
MAX_JSON_SAFE_INTEGER = 2**53 - 1
MAX_INCOMPLETE_REASON_CHARS = 4096

ARTIFACT_VERSIONS = {
    "order_book_jsonl_gz": "order-book-jsonl/1",
    "active_bid_prices_json": "active-bid-prices-json/1",
    "active_bid_prices_csv": "active-bid-prices-csv/1",
}

FIXED_ARTIFACT_PATHS = {
    "active_bid_prices_json": "data/active_bid_prices.json",
    "active_bid_prices_csv": "data/active_bid_prices.csv",
}

TOP_LEVEL_FIELDS = {
    "schema_version",
    "receipt_id",
    "run",
    "scan",
    "updates",
    "artifact_versions",
    "artifacts",
}
RUN_FIELDS = {"repository", "workflow", "run_id", "attempt", "started_at", "completed_at"}
SCAN_FIELDS = {"status", "pages_completed", "terminal_page_complete", "rows_observed", "incomplete_reason"}
UPDATE_FIELDS = {"candidates_observed", "eligible_updates", "no_eligible_updates"}
ARTIFACT_FIELDS = {"kind", "path", "sha256", "bytes"}

RECEIPT_ID_RE = re.compile(r"^bid-sync-([1-9][0-9]*)-([1-9][0-9]*)$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
ORDER_BOOK_PATH_RE = re.compile(
    r"^data/order_book/([0-9]{4})/([0-9]{2})/([0-9]{2})/([1-9][0-9]*)-([1-9][0-9]*)\.jsonl\.gz$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z$"
)


class DuplicateObjectKeyError(ValueError):
    pass


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateObjectKeyError("duplicate object key")
        result[key] = value
    return result


def loads_receipt_json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_reject_duplicate_object_keys)


def _error(errors: list[dict[str, str]], code: str, path: str, message: str) -> None:
    errors.append({"code": code, "path": path, "message": message})


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_object(
    value: Any,
    expected_fields: set[str],
    path: str,
    errors: list[dict[str, str]],
) -> bool:
    if not isinstance(value, dict):
        _error(errors, "invalid_type", path, "must be an object")
        return False
    if any(not isinstance(field, str) for field in value):
        _error(errors, "invalid_object_key", path, "object keys must be strings")
        return False
    for field in sorted(expected_fields - set(value)):
        _error(errors, "missing_field", f"{path}.{field}", "required field is missing")
    for field in sorted(set(value) - expected_fields):
        _error(errors, "unexpected_field", f"{path}.{field}", "field is outside the closed contract")
    return True


def _check_nonnegative_int(value: Any, path: str, errors: list[dict[str, str]], *, positive: bool = False) -> bool:
    if not _is_int(value):
        _error(errors, "invalid_type", path, "must be an integer")
        return False
    minimum = 1 if positive else 0
    if value < minimum or value > MAX_JSON_SAFE_INTEGER:
        _error(errors, "out_of_range", path, f"must be between {minimum} and {MAX_JSON_SAFE_INTEGER}")
        return False
    return True


def _parse_utc_timestamp(value: Any, path: str, errors: list[dict[str, str]]) -> datetime | None:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        _error(errors, "timestamp_must_be_utc", path, "must use RFC3339 UTC seconds ending in Z")
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _error(errors, "invalid_timestamp", path, "must be a valid calendar timestamp")
        return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def validate_receipt(receipt: Any) -> dict[str, Any]:
    """Validate one anonymous receipt and return a deterministic result."""

    errors: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "valid": False,
        "errors": errors,
        "receipt_id": None,
        "scan_complete": False,
        "partial_reason": None,
        "eligible_updates": 0,
        "no_eligible_updates": False,
    }

    if not isinstance(receipt, dict):
        _error(errors, "invalid_type", "$", "receipt must be an object")
        return result

    _check_object(receipt, TOP_LEVEL_FIELDS, "$", errors)

    schema_version = receipt.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        _error(
            errors,
            "unsupported_schema_version",
            "$.schema_version",
            f"must be {SCHEMA_VERSION}",
        )
        return result

    receipt_id = receipt.get("receipt_id")
    receipt_id_parts = None
    if not isinstance(receipt_id, str) or not (receipt_id_parts := RECEIPT_ID_RE.fullmatch(receipt_id)):
        _error(errors, "invalid_receipt_id", "$.receipt_id", "must be bid-sync-<run_id>-<attempt>")
    else:
        result["receipt_id"] = receipt_id

    run = receipt.get("run")
    started_at = None
    completed_at = None
    run_id = None
    attempt = None
    if _check_object(run, RUN_FIELDS, "$.run", errors):
        if run.get("repository") != REPOSITORY:
            _error(errors, "repository_mismatch", "$.run.repository", f"must be {REPOSITORY}")
        if run.get("workflow") != WORKFLOW:
            _error(errors, "workflow_mismatch", "$.run.workflow", f"must be {WORKFLOW}")
        run_id_value = run.get("run_id")
        if not isinstance(run_id_value, str) or not RUN_ID_RE.fullmatch(run_id_value):
            _error(errors, "invalid_run_id", "$.run.run_id", "must be positive decimal digits without leading zeroes")
        else:
            run_id = run_id_value
        if _check_nonnegative_int(run.get("attempt"), "$.run.attempt", errors, positive=True):
            attempt = run["attempt"]
        started_at = _parse_utc_timestamp(run.get("started_at"), "$.run.started_at", errors)
        completed_at = _parse_utc_timestamp(run.get("completed_at"), "$.run.completed_at", errors)
        if started_at is not None and completed_at is not None and completed_at < started_at:
            _error(errors, "invalid_time_order", "$.run.completed_at", "must not be before started_at")

    if receipt_id_parts is not None and run_id is not None and attempt is not None:
        if receipt_id_parts.group(1) != run_id or receipt_id_parts.group(2) != str(attempt):
            _error(errors, "receipt_id_mismatch", "$.receipt_id", "must exactly encode run_id and attempt")

    scan = receipt.get("scan")
    if _check_object(scan, SCAN_FIELDS, "$.scan", errors):
        status = scan.get("status")
        if not isinstance(status, str) or status not in {"complete", "partial"}:
            _error(errors, "invalid_scan_status", "$.scan.status", "must be complete or partial")
        _check_nonnegative_int(scan.get("pages_completed"), "$.scan.pages_completed", errors)
        _check_nonnegative_int(scan.get("rows_observed"), "$.scan.rows_observed", errors)
        terminal_page_complete = scan.get("terminal_page_complete")
        if not isinstance(terminal_page_complete, bool):
            _error(errors, "invalid_type", "$.scan.terminal_page_complete", "must be a boolean")
        incomplete_reason = scan.get("incomplete_reason")
        if incomplete_reason is not None:
            if not isinstance(incomplete_reason, str):
                _error(errors, "invalid_type", "$.scan.incomplete_reason", "must be null or a string")
            elif not incomplete_reason.strip():
                _error(errors, "empty_incomplete_reason", "$.scan.incomplete_reason", "must be non-empty when present")
            elif len(incomplete_reason) > MAX_INCOMPLETE_REASON_CHARS:
                _error(
                    errors,
                    "out_of_range",
                    "$.scan.incomplete_reason",
                    f"must be at most {MAX_INCOMPLETE_REASON_CHARS} characters",
                )
            elif any(unicodedata.category(char) == "Cc" for char in incomplete_reason):
                _error(errors, "control_character", "$.scan.incomplete_reason", "must not contain control characters")
        if status == "complete":
            if terminal_page_complete is not True or incomplete_reason is not None:
                _error(
                    errors,
                    "complete_scan_marked_incomplete",
                    "$.scan",
                    "a complete scan requires terminal_page_complete=true and incomplete_reason=null",
                )
        elif status == "partial":
            if terminal_page_complete is True and incomplete_reason is None:
                _error(
                    errors,
                    "partial_scan_missing_evidence",
                    "$.scan",
                    "a partial scan needs an incomplete terminal page or a reason",
                )
            result["partial_reason"] = incomplete_reason

    updates = receipt.get("updates")
    if _check_object(updates, UPDATE_FIELDS, "$.updates", errors):
        candidates_valid = _check_nonnegative_int(
            updates.get("candidates_observed"), "$.updates.candidates_observed", errors
        )
        eligible_valid = _check_nonnegative_int(
            updates.get("eligible_updates"), "$.updates.eligible_updates", errors
        )
        no_eligible = updates.get("no_eligible_updates")
        if not isinstance(no_eligible, bool):
            _error(errors, "invalid_type", "$.updates.no_eligible_updates", "must be a boolean")
        if eligible_valid:
            result["eligible_updates"] = updates["eligible_updates"]
        if candidates_valid and eligible_valid and updates["eligible_updates"] > updates["candidates_observed"]:
            _error(
                errors,
                "eligible_updates_exceed_candidates",
                "$.updates.eligible_updates",
                "must not exceed candidates_observed",
            )
        if isinstance(no_eligible, bool) and eligible_valid:
            expected_no_eligible = updates["eligible_updates"] == 0
            if no_eligible != expected_no_eligible:
                _error(
                    errors,
                    "eligible_update_flag_mismatch",
                    "$.updates.no_eligible_updates",
                    "must exactly match eligible_updates == 0",
                )
            result["no_eligible_updates"] = no_eligible

    artifact_versions = receipt.get("artifact_versions")
    if not isinstance(artifact_versions, dict):
        _error(errors, "invalid_type", "$.artifact_versions", "must be an object")
    else:
        if set(artifact_versions) != set(ARTIFACT_VERSIONS):
            _error(
                errors,
                "artifact_version_keys_mismatch",
                "$.artifact_versions",
                "must contain exactly the three public artifact kinds",
            )
        for kind, expected_version in ARTIFACT_VERSIONS.items():
            if artifact_versions.get(kind) != expected_version:
                _error(
                    errors,
                    "unsupported_artifact_version",
                    f"$.artifact_versions.{kind}",
                    f"must be {expected_version}",
                )

    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        _error(errors, "invalid_type", "$.artifacts", "must be an array")
    else:
        seen_paths: set[str] = set()
        seen_kinds: set[str] = set()
        for index, artifact in enumerate(artifacts):
            path_prefix = f"$.artifacts[{index}]"
            if not _check_object(artifact, ARTIFACT_FIELDS, path_prefix, errors):
                continue
            kind = artifact.get("kind")
            if not isinstance(kind, str) or kind not in ARTIFACT_VERSIONS:
                _error(errors, "unknown_artifact_kind", f"{path_prefix}.kind", "must be a public artifact kind")
                continue
            if kind in seen_kinds:
                _error(errors, "duplicate_artifact_kind", f"{path_prefix}.kind", "each artifact kind may appear once")
            seen_kinds.add(kind)

            artifact_path = artifact.get("path")
            if not isinstance(artifact_path, str) or not artifact_path:
                _error(errors, "invalid_type", f"{path_prefix}.path", "must be a non-empty string")
            elif artifact_path in seen_paths:
                _error(errors, "duplicate_artifact_path", f"{path_prefix}.path", "artifact paths must be unique")
            else:
                seen_paths.add(artifact_path)
                if kind in FIXED_ARTIFACT_PATHS:
                    if artifact_path != FIXED_ARTIFACT_PATHS[kind]:
                        _error(
                            errors,
                            "artifact_path_mismatch",
                            f"{path_prefix}.path",
                            f"must be {FIXED_ARTIFACT_PATHS[kind]}",
                        )
                else:
                    match = ORDER_BOOK_PATH_RE.fullmatch(artifact_path)
                    if not match:
                        _error(
                            errors,
                            "artifact_path_mismatch",
                            f"{path_prefix}.path",
                            "must match data/order_book/YYYY/MM/DD/<run_id>-<attempt>.jsonl.gz",
                        )
                    else:
                        try:
                            datetime.strptime(
                                f"{match.group(1)}-{match.group(2)}-{match.group(3)}", "%Y-%m-%d"
                            )
                        except ValueError:
                            _error(
                                errors,
                                "invalid_artifact_date",
                                f"{path_prefix}.path",
                                "order-book date directory must be a valid calendar date",
                            )
                    if match and run_id is not None and attempt is not None and (
                        match.group(4) != run_id or match.group(5) != str(attempt)
                    ):
                        _error(
                            errors,
                            "artifact_run_mismatch",
                            f"{path_prefix}.path",
                            "order-book path must encode the same run_id and attempt",
                        )

            sha256 = artifact.get("sha256")
            if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
                _error(errors, "invalid_sha256", f"{path_prefix}.sha256", "must be 64 lowercase hex characters")
            _check_nonnegative_int(artifact.get("bytes"), f"{path_prefix}.bytes", errors)

    if isinstance(scan, dict) and scan.get("status") == "complete":
        result["scan_complete"] = not any(
            error["code"] in {"complete_scan_marked_incomplete", "invalid_scan_status"} for error in errors
        )

    result["valid"] = not errors
    if not result["valid"]:
        result["scan_complete"] = False
    return result


def ingest_receipts(receipts: Iterable[Any]) -> dict[str, Any]:
    """Purely evaluate an ordered receipt stream for idempotent consumption.

    Exact repeated run receipts are idempotent. A repeated run/attempt with any
    payload difference is a conflict and is not accepted.
    """

    accepted: dict[tuple[str, int], str] = {}
    errors: list[dict[str, Any]] = []
    exact_duplicate_count = 0
    conflicting_duplicate_count = 0
    invalid_count = 0

    for index, receipt in enumerate(receipts):
        validation = validate_receipt(receipt)
        if not validation["valid"]:
            invalid_count += 1
            for error in validation["errors"]:
                errors.append({"index": index, **error})
            continue
        run = receipt["run"]
        key = (run["run_id"], run["attempt"])
        canonical = _canonical_json(receipt)
        previous = accepted.get(key)
        if previous is None:
            accepted[key] = canonical
        elif previous == canonical:
            exact_duplicate_count += 1
        else:
            conflicting_duplicate_count += 1
            errors.append(
                {
                    "index": index,
                    "code": "conflicting_duplicate_run",
                    "path": f"$[{index}]",
                    "message": "same run_id and attempt carry different receipt payloads",
                }
            )

    return {
        "valid_count": len(accepted),
        "exact_duplicate_count": exact_duplicate_count,
        "conflicting_duplicate_count": conflicting_duplicate_count,
        "invalid_count": invalid_count,
        "error_count": len(errors),
        "errors": errors,
    }
