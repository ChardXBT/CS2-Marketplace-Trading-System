"""Offline validation of GitHub Actions evidence for a bid-sync run.

The helpers in this module consume already-retrieved Actions run/job payloads
and an anonymous bid-sync receipt. They perform no network access and treat a
green workflow conclusion as insufficient unless the required execution steps
and expected output descriptors are also present.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from src.bid_sync_receipts.contract import WORKFLOW, validate_receipt

MONITOR_STEP_NAME = "Run the monitor script"
CHECKPOINT_STEP_NAME = "Checkpoint runtime changes"
REQUIRED_STEP_NAMES = (MONITOR_STEP_NAME, CHECKPOINT_STEP_NAME)
MAX_JSON_SAFE_INTEGER = 2**53 - 1
UTC_TIMESTAMP_RE = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z$"
)


def _error(errors: list[dict[str, str]], code: str, path: str, message: str) -> None:
    errors.append({"code": code, "path": path, "message": message})


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_JSON_SAFE_INTEGER


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_JSON_SAFE_INTEGER


def _parse_utc_timestamp(value: Any, path: str, errors: list[dict[str, str]]) -> datetime | None:
    if value is None:
        _error(errors, "missing_timestamp", path, "timestamp evidence is required")
        return None
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        _error(errors, "timestamp_must_be_utc", path, "must use RFC3339 UTC seconds ending in Z")
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _error(errors, "invalid_timestamp", path, "must be a valid calendar timestamp")
        return None


def validate_workflow_sync_evidence(run: Any, jobs_payload: Any, receipt: Any) -> dict[str, Any]:
    """Validate mocked or archived Actions evidence plus a sync receipt.

    GitHub API payloads are versioned externally, so unknown fields are
    tolerated. Required identity, execution, and output evidence fails closed.
    """

    errors: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "valid": False,
        "errors": errors,
        "run_id": None,
        "attempt": None,
        "artifact_kinds": [],
    }

    if not isinstance(run, dict):
        _error(errors, "invalid_type", "$.run", "run evidence must be an object")
        return result
    if not isinstance(jobs_payload, dict):
        _error(errors, "invalid_type", "$.jobs", "jobs evidence must be an object")
        return result

    receipt_validation = validate_receipt(receipt)
    if not receipt_validation["valid"]:
        _error(errors, "receipt_invalid", "$.receipt", "receipt must satisfy bid-sync-receipt/1")
        for error in receipt_validation["errors"]:
            nested = dict(error)
            nested_path = str(error.get("path", ""))
            nested["path"] = "$.receipt" + (nested_path[1:] if nested_path.startswith("$") else nested_path)
            errors.append(nested)
    elif not receipt_validation["scan_complete"]:
        _error(errors, "receipt_not_complete", "$.receipt.scan", "successful sync evidence requires a complete scan")
    else:
        result["artifact_kinds"] = sorted(
            artifact.get("kind") for artifact in receipt.get("artifacts", []) if isinstance(artifact, dict)
        )

    run_id_value = run.get("id")
    run_id = str(run_id_value) if _positive_int(run_id_value) else None
    if run_id is None:
        _error(errors, "invalid_run_id", "$.run.id", "run id must be a positive JSON-safe integer")
    else:
        result["run_id"] = run_id

    attempt_value = run.get("run_attempt")
    if not _positive_int(attempt_value):
        _error(errors, "invalid_attempt", "$.run.run_attempt", "run attempt must be a positive JSON-safe integer")
    else:
        result["attempt"] = attempt_value

    if run.get("name") != WORKFLOW:
        _error(errors, "workflow_mismatch", "$.run.name", f"must be {WORKFLOW}")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        _error(errors, "run_not_successful", "$.run", "run must be completed with conclusion success")

    run_created_at = _parse_utc_timestamp(run.get("created_at"), "$.run.created_at", errors)
    run_updated_at = _parse_utc_timestamp(run.get("updated_at"), "$.run.updated_at", errors)
    if run_created_at is not None and run_updated_at is not None and run_updated_at < run_created_at:
        _error(errors, "invalid_time_order", "$.run.updated_at", "must not be before created_at")

    if isinstance(receipt, dict) and isinstance(receipt.get("run"), dict):
        receipt_run = receipt["run"]
        if run_id is not None and receipt_run.get("run_id") != run_id:
            _error(errors, "run_id_mismatch", "$.run.id", "Actions run id must match the receipt")
        if _positive_int(attempt_value) and receipt_run.get("attempt") != attempt_value:
            _error(errors, "attempt_mismatch", "$.run.run_attempt", "Actions retry attempt must match the receipt")

    total_count = jobs_payload.get("total_count")
    if total_count is None:
        _error(errors, "missing_field", "$.jobs.total_count", "jobs evidence must include total_count")
    elif not _nonnegative_int(total_count):
        _error(errors, "invalid_type", "$.jobs.total_count", "total_count must be a non-negative integer")

    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        _error(errors, "invalid_type", "$.jobs.jobs", "jobs must be an array")
        result["valid"] = False
        return result
    if not jobs:
        _error(errors, "missing_job", "$.jobs.jobs", "sync evidence must include the monitor job")
        result["valid"] = False
        return result
    if len(jobs) != 1:
        _error(errors, "unexpected_job_count", "$.jobs.jobs", "the monitor workflow must have exactly one job")
        result["valid"] = False
        return result
    if _nonnegative_int(total_count) and total_count != len(jobs):
        _error(errors, "job_count_mismatch", "$.jobs.total_count", "total_count must match the returned jobs")

    job = jobs[0]
    if not isinstance(job, dict):
        _error(errors, "invalid_type", "$.jobs.jobs[0]", "job evidence must be an object")
        result["valid"] = False
        return result

    if not _positive_int(job.get("id")):
        _error(errors, "invalid_job_id", "$.jobs.jobs[0].id", "job id must be a positive integer")

    job_run_id = job.get("run_id")
    if not _positive_int(job_run_id):
        _error(errors, "invalid_job_run_id", "$.jobs.jobs[0].run_id", "job run id must be a positive integer")
    elif run_id is not None and str(job_run_id) != run_id:
        _error(errors, "job_run_mismatch", "$.jobs.jobs[0].run_id", "job must belong to the receipt run")

    if job.get("status") != "completed" or job.get("conclusion") != "success":
        _error(errors, "job_not_successful", "$.jobs.jobs[0]", "job must be completed with conclusion success")

    job_started_at = _parse_utc_timestamp(job.get("started_at"), "$.jobs.jobs[0].started_at", errors)
    job_completed_at = _parse_utc_timestamp(job.get("completed_at"), "$.jobs.jobs[0].completed_at", errors)
    if job_started_at is not None and job_completed_at is not None and job_completed_at < job_started_at:
        _error(errors, "invalid_time_order", "$.jobs.jobs[0].completed_at", "must not be before started_at")
    if run_created_at is not None and job_started_at is not None and job_started_at < run_created_at:
        _error(errors, "job_time_mismatch", "$.jobs.jobs[0].started_at", "job cannot start before its run is created")
    if run_updated_at is not None and job_completed_at is not None and job_completed_at > run_updated_at:
        _error(
            errors,
            "job_time_mismatch",
            "$.jobs.jobs[0].completed_at",
            "job cannot complete after its run was last updated",
        )

    steps = job.get("steps")
    if not isinstance(steps, list):
        _error(errors, "invalid_type", "$.jobs.jobs[0].steps", "steps must be an array")
    elif not steps:
        _error(errors, "zero_steps", "$.jobs.jobs[0].steps", "a successful sync cannot have zero executed steps")
        runner_name = job.get("runner_name")
        if job.get("runner_id") in (None, 0) and (not isinstance(runner_name, str) or not runner_name.strip()):
            _error(
                errors,
                "runner_not_provisioned",
                "$.jobs.jobs[0]",
                "zero steps with no runner identify a provisioning or billing failure, not a sync",
            )
    else:
        seen_names: set[str] = set()
        seen_numbers: set[int] = set()
        positions: dict[str, int] = {}
        previous_step_number = None
        for index, step in enumerate(steps):
            path = f"$.jobs.jobs[0].steps[{index}]"
            if not isinstance(step, dict):
                _error(errors, "invalid_type", path, "step evidence must be an object")
                continue

            number = step.get("number")
            if not _positive_int(number):
                _error(errors, "invalid_step_number", f"{path}.number", "step number must be a positive integer")
            else:
                if number in seen_numbers:
                    _error(errors, "duplicate_step_number", f"{path}.number", "step numbers must be unique")
                seen_numbers.add(number)
                if previous_step_number is not None and number <= previous_step_number:
                    _error(
                        errors,
                        "invalid_step_order",
                        f"{path}.number",
                        "step numbers must increase in response order",
                    )
                previous_step_number = number

            name = step.get("name")
            if not isinstance(name, str) or not name:
                _error(errors, "invalid_step_name", f"{path}.name", "step name must be a non-empty string")
                continue
            if name in seen_names:
                _error(errors, "duplicate_step", f"{path}.name", "step names must be unique in this workflow")
            seen_names.add(name)
            positions.setdefault(name, index)

        required_times: dict[str, tuple[datetime | None, datetime | None]] = {}
        for required_name in REQUIRED_STEP_NAMES:
            if required_name not in positions:
                _error(
                    errors,
                    "missing_required_step",
                    "$.jobs.jobs[0].steps",
                    f"{required_name!r} must be present",
                )
                continue
            step = steps[positions[required_name]]
            step_path = f"$.jobs.jobs[0].steps[{positions[required_name]}]"
            if step.get("status") != "completed" or step.get("conclusion") != "success":
                _error(
                    errors,
                    "required_step_not_successful",
                    step_path,
                    f"{required_name!r} must complete successfully",
                )
            started_at = _parse_utc_timestamp(step.get("started_at"), f"{step_path}.started_at", errors)
            completed_at = _parse_utc_timestamp(step.get("completed_at"), f"{step_path}.completed_at", errors)
            required_times[required_name] = (started_at, completed_at)
            if started_at is not None and completed_at is not None and completed_at < started_at:
                _error(errors, "invalid_time_order", f"{step_path}.completed_at", "must not be before started_at")
            if (
                job_started_at is not None
                and job_completed_at is not None
                and started_at is not None
                and completed_at is not None
                and (started_at < job_started_at or completed_at > job_completed_at)
            ):
                _error(errors, "step_time_mismatch", step_path, "required step times must be inside the job lifetime")
        if MONITOR_STEP_NAME in positions and CHECKPOINT_STEP_NAME in positions:
            if positions[MONITOR_STEP_NAME] >= positions[CHECKPOINT_STEP_NAME]:
                _error(
                    errors,
                    "invalid_step_order",
                    "$.jobs.jobs[0].steps",
                    "the checkpoint step must follow the monitor step",
                )
            monitor_times = required_times.get(MONITOR_STEP_NAME)
            checkpoint_times = required_times.get(CHECKPOINT_STEP_NAME)
            if monitor_times and checkpoint_times:
                monitor_completed = monitor_times[1]
                checkpoint_started = checkpoint_times[0]
                if (
                    monitor_completed is not None
                    and checkpoint_started is not None
                    and checkpoint_started < monitor_completed
                ):
                    _error(
                        errors,
                        "invalid_step_time_order",
                        "$.jobs.jobs[0].steps",
                        "the checkpoint step must not start before the monitor step completes",
                    )

    if receipt_validation["valid"] and isinstance(receipt, dict) and isinstance(receipt.get("run"), dict):
        try:
            receipt_started_at = datetime.strptime(receipt["run"]["started_at"], "%Y-%m-%dT%H:%M:%SZ")
            receipt_completed_at = datetime.strptime(receipt["run"]["completed_at"], "%Y-%m-%dT%H:%M:%SZ")
        except (KeyError, TypeError, ValueError):
            receipt_started_at = None
            receipt_completed_at = None
        if (
            job_started_at is not None
            and job_completed_at is not None
            and receipt_started_at is not None
            and receipt_completed_at is not None
            and (receipt_started_at < job_started_at or receipt_completed_at > job_completed_at)
        ):
            _error(
                errors,
                "receipt_time_mismatch",
                "$.receipt.run",
                "receipt run times must be inside the Actions job lifetime",
            )

    result["valid"] = not errors
    return result
