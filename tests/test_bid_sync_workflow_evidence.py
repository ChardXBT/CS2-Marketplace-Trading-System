import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.bid_sync_receipts.workflow_evidence import validate_workflow_sync_evidence


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "bid_sync_receipts"
MONITOR_STEP = "Run the monitor script"
CHECKPOINT_STEP = "Checkpoint runtime changes"


def load_receipt(name="complete_scan.json"):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def successful_run():
    return {
        "id": 4242424242,
        "name": "CSFloat Hourly Monitor",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "head_sha": "a" * 40,
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-01-02T03:07:05Z",
    }


def step(name, conclusion="success", number=1):
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "number": number,
        "started_at": "2026-01-02T03:04:05Z",
        "completed_at": "2026-01-02T03:05:05Z",
    }


def successful_jobs_payload():
    return {
        "total_count": 1,
        "jobs": [
            {
                "id": 9876543210,
                "run_id": 4242424242,
                "status": "completed",
                "conclusion": "success",
                "runner_id": 123,
                "runner_name": "github-actions-runner",
                "started_at": "2026-01-02T03:04:05Z",
                "completed_at": "2026-01-02T03:07:05Z",
                "steps": [
                    step("Checkout repository code", number=1),
                    step(MONITOR_STEP, number=2),
                    step(CHECKPOINT_STEP, number=3),
                ],
            }
        ],
    }


def valid_evidence():
    return successful_run(), successful_jobs_payload(), load_receipt()


class WorkflowEvidenceTests(unittest.TestCase):
    def assert_invalid(self, run, jobs_payload, receipt, *expected_codes):
        result = validate_workflow_sync_evidence(run, jobs_payload, receipt)
        self.assertFalse(result["valid"])
        codes = {error["code"] for error in result["errors"]}
        for code in expected_codes:
            self.assertIn(code, codes)
        return result

    def test_successful_run_with_expected_output_is_valid(self):
        run, jobs_payload, receipt = valid_evidence()
        result = validate_workflow_sync_evidence(run, jobs_payload, receipt)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["run_id"], "4242424242")
        self.assertEqual(result["attempt"], 1)
        self.assertIn("active_bid_prices_json", result["artifact_kinds"])
        self.assertIn("active_bid_prices_csv", result["artifact_kinds"])

    def test_green_overall_run_is_not_success_without_checkpoint_output_step(self):
        run, jobs_payload, receipt = valid_evidence()
        jobs_payload["jobs"][0]["steps"] = [
            item for item in jobs_payload["jobs"][0]["steps"] if item["name"] != CHECKPOINT_STEP
        ]
        self.assert_invalid(run, jobs_payload, receipt, "missing_required_step")

    def test_green_overall_run_is_not_success_without_expected_snapshot_artifacts(self):
        run, jobs_payload, receipt = valid_evidence()
        receipt["artifacts"] = []
        self.assert_invalid(run, jobs_payload, receipt, "missing_required_artifact", "receipt_invalid")

    def test_zero_step_runner_provisioning_or_billing_failure_is_rejected(self):
        run, jobs_payload, receipt = valid_evidence()
        job = jobs_payload["jobs"][0]
        job["steps"] = []
        job["runner_id"] = 0
        job["runner_name"] = ""
        self.assert_invalid(run, jobs_payload, receipt, "zero_steps", "runner_not_provisioned")

    def test_missing_null_empty_and_wrong_type_evidence_fail_closed(self):
        for bad_run in (None, [], "run"):
            run, jobs_payload, receipt = valid_evidence()
            self.assert_invalid(bad_run, jobs_payload, receipt, "invalid_type")
        run, jobs_payload, receipt = valid_evidence()
        self.assert_invalid({}, jobs_payload, receipt, "invalid_run_id", "invalid_attempt")
        run, jobs_payload, receipt = valid_evidence()
        self.assert_invalid(run, {}, receipt, "invalid_type")
        run, jobs_payload, receipt = valid_evidence()
        self.assert_invalid(run, {"jobs": []}, receipt, "missing_job")

    def test_retry_attempt_is_accepted_only_when_receipt_matches_it(self):
        run, jobs_payload, receipt = valid_evidence()
        run["run_attempt"] = 2
        receipt["run"]["attempt"] = 2
        receipt["receipt_id"] = "bid-sync-4242424242-2"
        for artifact in receipt["artifacts"]:
            if artifact["kind"] == "order_book_jsonl_gz":
                artifact["path"] = artifact["path"].replace("-1.jsonl.gz", "-2.jsonl.gz")
        result = validate_workflow_sync_evidence(run, jobs_payload, receipt)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["attempt"], 2)

        run, jobs_payload, receipt = valid_evidence()
        run["run_attempt"] = 2
        self.assert_invalid(run, jobs_payload, receipt, "attempt_mismatch")

    def test_timeout_cancelled_or_failed_required_steps_are_rejected(self):
        for conclusion in ("failure", "cancelled", "timed_out", "skipped", None):
            run, jobs_payload, receipt = valid_evidence()
            for item in jobs_payload["jobs"][0]["steps"]:
                if item["name"] == MONITOR_STEP:
                    item["conclusion"] = conclusion
            self.assert_invalid(run, jobs_payload, receipt, "required_step_not_successful")

    def test_duplicate_jobs_and_duplicate_required_steps_fail_closed(self):
        run, jobs_payload, receipt = valid_evidence()
        jobs_payload["jobs"].append(copy.deepcopy(jobs_payload["jobs"][0]))
        jobs_payload["total_count"] = 2
        self.assert_invalid(run, jobs_payload, receipt, "unexpected_job_count")

        run, jobs_payload, receipt = valid_evidence()
        jobs_payload["jobs"][0]["steps"].append(step(MONITOR_STEP))
        self.assert_invalid(run, jobs_payload, receipt, "duplicate_step")

    def test_incomplete_or_inconsistent_jobs_response_is_rejected(self):
        run, jobs_payload, receipt = valid_evidence()
        del jobs_payload["total_count"]
        self.assert_invalid(run, jobs_payload, receipt, "missing_field")

        run, jobs_payload, receipt = valid_evidence()
        jobs_payload["total_count"] = 2
        self.assert_invalid(run, jobs_payload, receipt, "job_count_mismatch")

        run, jobs_payload, receipt = valid_evidence()
        del jobs_payload["jobs"][0]["id"]
        self.assert_invalid(run, jobs_payload, receipt, "invalid_job_id")

    def test_checkpoint_before_monitor_is_rejected_as_stale_or_reordered_evidence(self):
        run, jobs_payload, receipt = valid_evidence()
        steps = jobs_payload["jobs"][0]["steps"]
        monitor_index = next(index for index, item in enumerate(steps) if item["name"] == MONITOR_STEP)
        checkpoint_index = next(index for index, item in enumerate(steps) if item["name"] == CHECKPOINT_STEP)
        steps[monitor_index], steps[checkpoint_index] = steps[checkpoint_index], steps[monitor_index]
        self.assert_invalid(run, jobs_payload, receipt, "invalid_step_order")

    def test_stale_or_mismatched_run_identity_is_rejected(self):
        run, jobs_payload, receipt = valid_evidence()
        run["id"] = 4242424243
        self.assert_invalid(run, jobs_payload, receipt, "run_id_mismatch")

        run, jobs_payload, receipt = valid_evidence()
        jobs_payload["jobs"][0]["run_id"] = 4242424243
        self.assert_invalid(run, jobs_payload, receipt, "job_run_mismatch")

    def test_incomplete_or_nonsuccess_run_status_is_rejected(self):
        for status, conclusion in (
            ("in_progress", None),
            ("completed", "failure"),
            ("completed", "cancelled"),
            ("completed", None),
        ):
            run, jobs_payload, receipt = valid_evidence()
            run["status"] = status
            run["conclusion"] = conclusion
            self.assert_invalid(run, jobs_payload, receipt, "run_not_successful")


class WorkflowEvidenceCliTests(unittest.TestCase):
    def write_json(self, directory, name, value):
        path = Path(directory) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def run_cli(self, *paths):
        repo_root = Path(__file__).resolve().parents[1]
        return subprocess.run(
            [sys.executable, str(repo_root / "tools" / "bid_sync_workflow_evidence.py"), *map(str, paths)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_cli_accepts_valid_mocked_actions_responses(self):
        run, jobs_payload, receipt = valid_evidence()
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = [
                self.write_json(temp_dir, "run.json", run),
                self.write_json(temp_dir, "jobs.json", jobs_payload),
                self.write_json(temp_dir, "receipt.json", receipt),
            ]
            completed = self.run_cli(*paths)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        summary = json.loads(completed.stdout)
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["run_id"], "4242424242")

    def test_cli_rejects_zero_step_and_missing_output_without_traceback(self):
        run, jobs_payload, receipt = valid_evidence()
        jobs_payload["jobs"][0]["steps"] = []
        jobs_payload["jobs"][0]["runner_id"] = 0
        jobs_payload["jobs"][0]["runner_name"] = ""
        receipt["artifacts"] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = [
                self.write_json(temp_dir, "run.json", run),
                self.write_json(temp_dir, "jobs.json", jobs_payload),
                self.write_json(temp_dir, "receipt.json", receipt),
            ]
            completed = self.run_cli(*paths)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        summary = json.loads(completed.stdout)
        codes = {error["code"] for error in summary["errors"]}
        self.assertIn("zero_steps", codes)
        self.assertIn("runner_not_provisioned", codes)
        self.assertIn("missing_required_artifact", codes)

    def test_cli_rejects_unreadable_evidence_without_traceback(self):
        run, jobs_payload, receipt = valid_evidence()
        with tempfile.TemporaryDirectory() as temp_dir:
            bad = Path(temp_dir) / "run.json"
            bad.write_text("{bad json", encoding="utf-8")
            paths = [
                bad,
                self.write_json(temp_dir, "jobs.json", jobs_payload),
                self.write_json(temp_dir, "receipt.json", receipt),
            ]
            completed = self.run_cli(*paths)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "")
        summary = json.loads(completed.stdout)
        self.assertEqual(summary["errors"][0]["code"], "unreadable_evidence")


if __name__ == "__main__":
    unittest.main()
