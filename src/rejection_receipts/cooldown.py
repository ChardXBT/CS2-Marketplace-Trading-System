"""Pure, disabled consumer for souvenir rejection-receipt recreation holds.

This module deliberately performs no marketplace, network, process, or trading
action.  It validates local receipt objects, persists them durably when a
caller explicitly asks the store to append, and returns a dry-run eligibility
decision for one exact order target.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback; CI is Linux.
    fcntl = None

SCHEMA = "souvenir_rejection_receipt/1"
HOLD_TYPE = "synthetic_recreate_hold"
HOLD_SCOPE = "order_target_only"
HOLD_DURATION = timedelta(hours=72)
MAX_CLOCK_SKEW = timedelta(minutes=2)
CONSUMER_STATE = "disabled"

CONFIRMED_VERDICT = "confirmed_mismatch"
MANUAL_VERDICT = "manual_review"
MISMATCH_REASONS = {"missing_sticker", "damaged_sticker", "wrong_asset", "wrong_item", "none"}
HOLD_STATES = {"proposed_not_live"}

_INSTANT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_ID_RE = re.compile(r"^\S{8,128}$")
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,199}$")

_REQUIRED_FIELDS = {
    "schema",
    "receipt_id",
    "detected_at",
    "account",
    "provider",
    "steam_offer_id",
    "steam_trade_id",
    "marketplace_trade_id",
    "contract_id",
    "transaction_id",
    "buy_order_id",
    "order_target",
    "asset_id",
    "market_hash_name",
    "expected_evidence",
    "actual_evidence",
    "verdict",
    "mismatch",
    "hold",
}
_NULLABLE_ID_FIELDS = ("steam_trade_id", "marketplace_trade_id", "contract_id", "transaction_id", "buy_order_id")
_OPTIONAL_FIELDS = {"reason"}
_TARGET_FIELDS = {"marketplace", "account", "target_key", "expression_sha256"}


class ReceiptValidationError(ValueError):
    """A receipt is outside the coordinated contract."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class ReceiptConflictError(ValueError):
    """Two different payloads claim the same durable receipt identity."""

    def __init__(self, reason: str = "conflicting_duplicate_receipt"):
        self.reason = reason
        super().__init__(reason)


class ReceiptStoreCorruptionError(ValueError):
    """The append-only ledger cannot be replayed safely."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _fail(reason: str) -> None:
    raise ReceiptValidationError(reason)


def _coerce_now(now: Optional[Any]) -> datetime:
    value = now() if callable(now) else now
    if value is None:
        value = datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be an aware datetime")
    return value.astimezone(timezone.utc)


def parse_instant(value: Any, field: str = "instant") -> datetime:
    """Parse the RFC3339 UTC/offset shape used by the receipt contract."""

    if not isinstance(value, str) or not _INSTANT_RE.fullmatch(value):
        _fail(f"invalid_{field}")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        _fail(f"invalid_{field}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(f"invalid_{field}")
    return parsed.astimezone(timezone.utc)


def format_instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _object(value: Any, reason: str) -> dict:
    if not isinstance(value, dict):
        _fail(reason)
    return value


def _nonempty_string(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(reason)
    return value


def _nullable_id(value: Any, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not value):
        _fail(f"invalid_{field}")


def _sha256(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _fail(reason)
    return value


def canonical_json(value: Any) -> str:
    """Stable JSON representation used only for exact duplicate comparison."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class TargetIdentity:
    """Exact account-scoped order target a recreation hold may cover."""

    marketplace: str
    account: str
    target_key: str
    expression_sha256: str

    def __post_init__(self) -> None:
        for field in ("marketplace", "account", "target_key"):
            if not isinstance(getattr(self, field), str) or not getattr(self, field):
                raise ValueError(f"{field} must be a non-empty string")
        if not isinstance(self.expression_sha256, str) or not _SHA256_RE.fullmatch(self.expression_sha256):
            raise ValueError("expression_sha256 must be a lowercase sha256 hex digest")

    def to_dict(self) -> dict:
        return {
            "marketplace": self.marketplace,
            "account": self.account,
            "target_key": self.target_key,
            "expression_sha256": self.expression_sha256,
        }


@dataclass(frozen=True)
class Hold:
    receipt_id: str
    identity: TargetIdentity
    mismatch: str
    detected_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class EligibilityDecision:
    operation: str
    decision: str
    applied: bool
    consumer_state: str
    identity: TargetIdentity
    receipt_ids: Tuple[str, ...]
    reasons: Tuple[str, ...]
    expires_at: Optional[datetime]

    def to_dict(self) -> dict:
        return {
            "operation": self.operation,
            "decision": self.decision,
            "applied": self.applied,
            "consumer_state": self.consumer_state,
            "identity": self.identity.to_dict(),
            "receipt_ids": list(self.receipt_ids),
            "reasons": list(self.reasons),
            "expires_at": format_instant(self.expires_at) if self.expires_at is not None else None,
        }


@dataclass(frozen=True)
class AppendResult:
    written: bool
    duplicate: bool
    receipt_id: str


def _validate_evidence(receipt: dict, field: str) -> None:
    evidence = _object(receipt.get(field), f"invalid_{field}")
    if set(evidence).difference({"source", "digest"}):
        _fail("unexpected_evidence_field")
    _nonempty_string(evidence.get("source"), f"invalid_{field}_source")
    _sha256(evidence.get("digest"), f"invalid_{field}_digest")


def validate_receipt(receipt: Any, *, now: Optional[Any] = None) -> dict:
    """Validate the coordinated receipt envelope without contacting anything.

    The consumer treats ``receipt_id`` as an opaque stable identity.  It does
    not invent strategy data or accept account-wide holds: a hold-bearing
    confirmed receipt must carry exact target identity and a 72-hour
    recreation-only hold whose expiry is exactly ``detected_at + 72h``.
    Confirmed or manual-review evidence without a hold remains durable evidence
    but never affects eligibility.
    """

    checked_at = _coerce_now(now)
    if not isinstance(receipt, dict):
        _fail("invalid_receipt")
    missing = sorted(_REQUIRED_FIELDS.difference(receipt))
    if missing:
        _fail(f"missing_{missing[0]}")
    if set(receipt).difference(_REQUIRED_FIELDS | _OPTIONAL_FIELDS):
        _fail("unexpected_receipt_field")
    if receipt["schema"] != SCHEMA:
        _fail("unsupported_schema")
    receipt_id = receipt["receipt_id"]
    if not isinstance(receipt_id, str) or not _RECEIPT_ID_RE.fullmatch(receipt_id):
        _fail("invalid_receipt_id")

    detected_at = parse_instant(receipt["detected_at"], "detected_at")
    if detected_at > checked_at + MAX_CLOCK_SKEW:
        _fail("future_receipt")

    _nonempty_string(receipt["account"], "invalid_account")
    _nonempty_string(receipt["provider"], "invalid_provider")
    _nonempty_string(receipt["steam_offer_id"], "invalid_steam_offer_id")
    for field in _NULLABLE_ID_FIELDS:
        _nullable_id(receipt[field], field)
    _nonempty_string(receipt["asset_id"], "invalid_asset_id")
    _nonempty_string(receipt["market_hash_name"], "invalid_market_hash_name")
    if "reason" in receipt and (not isinstance(receipt["reason"], str) or not _REASON_RE.fullmatch(receipt["reason"])):
        _fail("invalid_reason")
    _validate_evidence(receipt, "expected_evidence")
    _validate_evidence(receipt, "actual_evidence")

    verdict = receipt["verdict"]
    if verdict not in {CONFIRMED_VERDICT, MANUAL_VERDICT}:
        _fail("invalid_verdict")
    mismatch = receipt["mismatch"]
    if mismatch not in MISMATCH_REASONS:
        _fail("invalid_mismatch")

    target_row = receipt["order_target"]
    target = None
    target_key = None
    expression = None
    if target_row is not None:
        target = _object(target_row, "invalid_order_target")
        if set(target).difference(_TARGET_FIELDS):
            _fail("unexpected_order_target_field")
        missing_target_fields = sorted(_TARGET_FIELDS.difference(target))
        if missing_target_fields:
            _fail(f"missing_order_target_{missing_target_fields[0]}")
        for field in ("marketplace", "account"):
            _nonempty_string(target.get(field), f"invalid_order_target_{field}")
        target_key = target.get("target_key")
        expression = target.get("expression_sha256")
        if target_key is not None and (not isinstance(target_key, str) or not target_key):
            _fail("invalid_order_target_key")
        if expression is not None:
            _sha256(expression, "invalid_expression_sha256")
        if (target_key is None) != (expression is None):
            _fail("incomplete_order_target")
        if target.get("marketplace") != receipt["provider"]:
            _fail("order_target_marketplace_mismatch")
        if target.get("account") != receipt["account"]:
            _fail("order_target_account_mismatch")

    hold = receipt["hold"]
    if verdict == MANUAL_VERDICT:
        if hold is not None:
            _fail("manual_review_hold_forbidden")
        return receipt

    if mismatch == "none":
        _fail("mismatch_required")
    if hold is None:
        return receipt
    if target is None or target_key is None or expression is None:
        _fail("incomplete_order_target")
    hold_object = _object(hold, "invalid_hold")
    if set(hold_object).difference({"type", "duration_hours", "expires_at", "scope", "state"}):
        _fail("unexpected_hold_field")
    if hold_object.get("type") != HOLD_TYPE:
        _fail("invalid_hold_type")
    if type(hold_object.get("duration_hours")) is not int or hold_object["duration_hours"] != 72:
        _fail("invalid_hold_duration")
    if hold_object.get("scope") != HOLD_SCOPE:
        _fail("invalid_hold_scope")
    if hold_object.get("state") not in HOLD_STATES:
        _fail("invalid_hold_state")
    expires_at = parse_instant(hold_object.get("expires_at"), "hold_expires_at")
    if expires_at != detected_at + HOLD_DURATION:
        _fail("hold_expiry_mismatch")
    return receipt


def _identity_from_target(target: dict) -> TargetIdentity:
    return TargetIdentity(
        marketplace=target["marketplace"],
        account=target["account"],
        target_key=target["target_key"],
        expression_sha256=target["expression_sha256"],
    )


def _deduplicate_validated(receipts: Iterable[dict], *, now: Optional[Any] = None) -> Tuple[dict, ...]:
    checked_at = _coerce_now(now)
    unique = []
    seen = {}
    for receipt in receipts:
        validate_receipt(receipt, now=checked_at)
        receipt_id = receipt["receipt_id"]
        canonical = canonical_json(receipt)
        if receipt_id in seen:
            if seen[receipt_id] != canonical:
                raise ReceiptConflictError()
            continue
        seen[receipt_id] = canonical
        unique.append(receipt)
    return tuple(unique)


def holds_from_receipts(receipts: Iterable[dict], *, now: Optional[Any] = None) -> Tuple[Hold, ...]:
    """Build unique confirmed holds from in-memory receipts; performs no I/O."""

    checked_at = _coerce_now(now)
    holds = []
    for receipt in _deduplicate_validated(receipts, now=checked_at):
        if receipt["verdict"] != CONFIRMED_VERDICT or receipt["hold"] is None:
            continue
        hold = receipt["hold"]
        holds.append(Hold(
            receipt_id=receipt["receipt_id"],
            identity=_identity_from_target(receipt["order_target"]),
            mismatch=receipt["mismatch"],
            detected_at=parse_instant(receipt["detected_at"], "detected_at"),
            expires_at=parse_instant(hold["expires_at"], "hold_expires_at"),
        ))
    return tuple(holds)


def evaluate_recreation(identity: TargetIdentity, receipts: Iterable[dict], now: Optional[Any] = None) -> EligibilityDecision:
    """Return the disabled dry-run recreation decision for one exact target.

    ``applied`` is always false.  This is intentional: the result is evidence
    for a future release-controlled integration, never permission to mutate an
    order by itself.
    """

    checked_at = _coerce_now(now)
    active = [
        hold for hold in holds_from_receipts(receipts, now=checked_at)
        if hold.identity == identity and checked_at < hold.expires_at
    ]
    return EligibilityDecision(
        operation="recreate",
        decision="hold" if active else "allow",
        applied=False,
        consumer_state=CONSUMER_STATE,
        identity=identity,
        receipt_ids=tuple(hold.receipt_id for hold in active),
        reasons=tuple(sorted({hold.mismatch for hold in active})),
        expires_at=max((hold.expires_at for hold in active), default=None),
    )


class ReceiptStore:
    """Append-only local JSONL receipt ledger.

    The store is durable input for the pure decision function.  It never writes
    unless ``append`` is called; loading and dry-run evaluation are read-only.
    Cooperating writers on Unix are serialized with ``flock``.  Replays fail
    closed on malformed JSON, non-object rows, or conflicting duplicate IDs.
    """

    def __init__(self, path: os.PathLike | str, *, now: Optional[Callable[[], datetime]] = None):
        self.path = Path(path)
        self._now = now

    def _checked_now(self) -> datetime:
        return _coerce_now(self._now)

    @staticmethod
    def _lock(handle: Any, exclusive: bool) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)

    @staticmethod
    def _unlock(handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _parse_rows(self, raw: bytes) -> Tuple[dict, ...]:
        if not raw:
            return ()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReceiptStoreCorruptionError("corrupt_ledger_encoding") from exc
        rows = []
        seen = {}
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line:
                raise ReceiptStoreCorruptionError(f"corrupt_ledger_line_{line_number}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReceiptStoreCorruptionError(f"corrupt_ledger_line_{line_number}") from exc
            if not isinstance(row, dict):
                raise ReceiptStoreCorruptionError(f"corrupt_ledger_line_{line_number}")
            validate_receipt(row, now=self._checked_now())
            receipt_id = row["receipt_id"]
            canonical = canonical_json(row)
            if receipt_id in seen:
                if seen[receipt_id] != canonical:
                    raise ReceiptConflictError()
                continue
            seen[receipt_id] = canonical
            rows.append(row)
        return tuple(rows)

    def load(self) -> Tuple[dict, ...]:
        if not self.path.exists():
            return ()
        with self.path.open("rb") as handle:
            self._lock(handle, exclusive=False)
            try:
                raw = handle.read()
            finally:
                self._unlock(handle)
        return self._parse_rows(raw)

    def append(self, receipt: dict) -> AppendResult:
        checked_at = self._checked_now()
        validate_receipt(receipt, now=checked_at)
        try:
            line = (canonical_json(receipt) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ReceiptValidationError("invalid_receipt_json") from exc

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as handle:
            self._lock(handle, exclusive=True)
            try:
                handle.seek(0)
                existing = self._parse_rows(handle.read())
                receipt_id = receipt["receipt_id"]
                canonical = canonical_json(receipt)
                for row in existing:
                    if row["receipt_id"] != receipt_id:
                        continue
                    if canonical_json(row) == canonical:
                        return AppendResult(written=False, duplicate=True, receipt_id=receipt_id)
                    raise ReceiptConflictError()
                handle.seek(0, os.SEEK_END)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
                return AppendResult(written=True, duplicate=False, receipt_id=receipt_id)
            finally:
                self._unlock(handle)
