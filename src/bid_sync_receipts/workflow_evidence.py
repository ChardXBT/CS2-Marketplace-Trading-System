"""Offline validation of GitHub Actions evidence for a bid-sync run.

The helpers in this module consume already-retrieved Actions run/job payloads
and an anonymous bid-sync receipt. They perform no network access and treat a
green workflow conclusion as insufficient unless the required execution steps
and expected output descriptors are also present.
"""

from __future__ import annotations

from typing import Any

from src.bid_sync_receipts.contract import WORKFLOW, validate_receipt

MONITOR_STEP_NAME = "Run the monitor script"
CHECKPOINT_STEP_NAME = "Checkpoint runtime changes"
REQUIRED_STEP_NAMES = (MONITOR_STEP_NAME, CHECKPOINT_STEP_NAME)
MAX_JSON_SAFE_INTEGER = 2**53 - 1


def _error(errors: list[dict[str, str]], code: str, path: str, message: str) -> None:
    errors.append({"code": code, "path": path, "message": message})


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_JSON_SAFE_INTEGER


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_JSON_SAFE_INTEGER


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
        positions: dict[str, int] = {}
        for index, step in enumerate(steps):
            path = f"$.jobs.jobs[0].steps[{index}]"
            if not isinstance(step, dict):
                _error(errors, "invalid_type", path, "step evidence must be an object")
                continue
            name = step.get("name")
            if not isinstance(name, str) or not name:
                _error(errors, "invalid_step_name", f"{path}.name", "step name must be a non-empty string")
                continue
            if name in seen_names:
                _error(errors, "duplicate_step", f"{path}.name", "step names must be unique in this workflow")
            seen_names.add(name)
            positions.setdefault(name, index)

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
            if step.get("status") != "completed" or step.get("conclusion") != "success":
                _error(
                    errors,
                    "required_step_not_successful",
                    f"$.jobs.jobs[0].steps[{positions[required_name]}]",
                    f"{required_name!r} must complete successfully",
                )
        if MONITOR_STEP_NAME in positions and CHECKPOINT_STEP_NAME in positions:
            if positions[MONITOR_STEP_NAME] >= positions[CHECKPOINT_STEP_NAME]:
                _error(
                    errors,
                    "invalid_step_order",
                    "$.jobs.jobs[0].steps",
                    "the checkpoint step must follow the monitor step",
                )

    result["valid"] = not errors
    return result
