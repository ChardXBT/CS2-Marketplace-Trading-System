# Code review style guide

These rules matter more than generic style. Flag violations at MEDIUM severity or higher.
Only comment on code changed by this PR — don't review untouched context.

## Money safety (highest priority)

- Any code path that lists, buys, bids, reprices, or transfers items must be
  fail-closed: on uncertainty, error, or missing data, it must do nothing.
- Dry-run / staged-OFF defaults must be preserved. Flag anything that weakens,
  bypasses, or defaults-around a safety gate.
- Marketplace mutations go through the bot's queue system. Flag direct writes
  that skip the queue.
- Hardcoded prices, limits, or thresholds that used to be config-driven are a
  regression — flag them.

## Secrets & environment

- Never suggest logging tokens, cookies, API keys, or session data — including
  in debug or error paths.
- Secrets come from env vars / secret stores only. Flag secrets in code,
  config files, comments, or test fixtures.
- Do not suggest reading or decrypting git-crypt-protected files.

## Reliability

- Notification/Discord/alerting failures must never crash the main loop —
  they get logged and swallowed, not raised.
- Long-running bots need explicit rate-limit handling and idempotent retries;
  flag retry loops without backoff or without a max-attempts cap.
- Flag unbounded growth: files, logs, queues, or in-memory state that grows
  without rotation or eviction.

## Repo-specific

- If this repository is one half of a mirrored pair (e.g., a Primary/Secondary
  twin), flag changes whose behavior would also need to be applied to the twin.
- Windows-first environment: flag unix-only assumptions (paths, signals,
  process APIs) that would break on Windows.

## Style

- Match the file's existing conventions over external style rules.
- Suggest only committable, minimal fixes — no drive-by refactors.
