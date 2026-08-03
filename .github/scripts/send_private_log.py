#!/usr/bin/env python3
"""Send the encrypted-run log to Discord without echoing secrets to Actions."""

from __future__ import annotations

import os
import re
import time
import urllib.parse

import requests


def redact_secrets(value: str, environment: dict[str, str] | None = None) -> str:
    """Remove configured secrets and Discord webhook paths from log text."""
    env = os.environ if environment is None else environment
    secrets: set[str] = set()
    for name in (
        "CSFLOAT_API_KEY",
        "DISCORD_WEBHOOK_URL",
        "DISCORD_LOG_WEBHOOK_URL",
    ):
        secret = str(env.get(name, "") or "").strip()
        if len(secret) >= 8:
            secrets.add(secret)
        if "WEBHOOK" in name and secret:
            parsed = urllib.parse.urlsplit(secret)
            for fragment in (
                parsed.path,
                parsed.path.rsplit("/", 1)[-1],
                f"{parsed.netloc}{parsed.path}",
            ):
                if len(fragment) >= 8:
                    secrets.add(fragment)

    redacted = value
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = re.sub(
        r"https?://(?:(?:canary|ptb)\.)?(?:discord(?:app)?\.com)/api/webhooks/"
        r"[0-9]+/[A-Za-z0-9._-]+",
        "[REDACTED_DISCORD_WEBHOOK]",
        redacted,
        flags=re.IGNORECASE,
    )
    redacted = re.sub(
        r"/api/webhooks/[0-9]+/[A-Za-z0-9._-]+",
        "/api/webhooks/[REDACTED]",
        redacted,
        flags=re.IGNORECASE,
    )
    return redacted


def main() -> int:
    webhook = os.environ.get("DISCORD_LOG_WEBHOOK_URL", "").strip()
    if not webhook:
        raise SystemExit("DISCORD_LOG_WEBHOOK_URL is not configured")

    try:
        log_path = os.environ.get("RUNTIME_PRIVATE_LOG", "/tmp/bot_output.txt")
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            log = handle.read()
    except OSError as exc:
        log = f"[WORKFLOW] Private run log was unavailable: {type(exc).__name__}"

    if not log.strip():
        log = "(no output)"
    status = (
        f"job={os.environ.get('JOB_STATUS', 'unknown')} "
        f"admission={os.environ.get('ADMISSION_OUTCOME', 'unknown')} "
        f"checkout={os.environ.get('CHECKOUT_OUTCOME', 'unknown')} "
        f"unlock={os.environ.get('UNLOCK_OUTCOME', 'unknown')} "
        f"validation={os.environ.get('VALIDATION_OUTCOME', 'unknown')} "
        f"monitor={os.environ.get('MONITOR_OUTCOME', 'unknown')} "
        f"checkpoint={os.environ.get('CHECKPOINT_OUTCOME', 'unknown')}"
    )
    run_url = os.environ.get("RUN_URL", "")
    log = redact_secrets(f"[WORKFLOW STATUS] {status}\n[RUN] {run_url}\n\n{log}")

    max_chars = 54_000
    if len(log) > max_chars:
        omitted = len(log) - max_chars
        log = (
            log[:9_000]
            + f"\n\n... {omitted} log characters omitted ...\n\n"
            + log[-45_000:]
        )
    chunks = [log.replace("```", "'''")[i:i + 1800] for i in range(0, len(log), 1800)]
    chunks = chunks or ["(no output)"]
    failed_chunks = 0
    for index, chunk in enumerate(chunks):
        message = f"**📋 Bot Log [{index + 1}/{len(chunks)}]**\n```\n{chunk}\n```"
        sent = False
        for attempt in range(5):
            wait = min(15, 2**attempt)
            try:
                # Keep the requests transport that delivered this webhook before
                # the private-log hardening change. Discord returns 403 to the
                # runner's default Python-urllib user agent, while the same
                # existing webhook accepts requests' established client profile.
                response = requests.post(
                    webhook,
                    json={"content": message, "allowed_mentions": {"parse": []}},
                    timeout=15,
                )
                status_code = response.status_code
                if status_code in (200, 204):
                    sent = True
                    break
                if status_code == 429:
                    try:
                        body = response.json()
                        retry_after = float(body.get("retry_after", 2))
                    except (ValueError, TypeError):
                        retry_after = 2
                    wait = min(60, max(1, retry_after + 1))
                if 400 <= status_code < 500 and status_code != 429:
                    print(
                        f"Discord HTTP {status_code}; private log endpoint "
                        f"rejected chunk {index + 1}"
                    )
                    break
                print(
                    f"Discord HTTP {status_code}; retrying chunk {index + 1} "
                    f"in {wait}s ({attempt + 1}/5)"
                )
            except (requests.RequestException, OSError, TimeoutError, ValueError) as exc:
                print(
                    f"Discord transport error {type(exc).__name__}; retrying "
                    f"chunk {index + 1} in {wait}s ({attempt + 1}/5)"
                )
            if attempt < 4:
                time.sleep(wait)
        if not sent:
            failed_chunks += 1
            print(f"Chunk {index + 1} was not delivered")
        if index + 1 < len(chunks):
            time.sleep(1)

    if failed_chunks:
        print(
            "::warning title=Private Discord log unavailable::"
            "The monitor result is authoritative; update the dedicated "
            "DISCORD_LOG_WEBHOOK_URL secret to restore private log delivery."
        )
        raise SystemExit(
            f"Discord delivery failed for {failed_chunks}/{len(chunks)} chunks"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
