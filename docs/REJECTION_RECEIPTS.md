# Disabled rejection-receipt cooldown consumer

This document is the public, strategy-free contract for the consumer added in
`src/rejection_receipts/`. It uses anonymous fixture identities only. It does
not enable trading behavior, publish target data, or change prices, caps,
rates, budgets, or account policy.

## Boundary shared with the acceptor

The coordinated boundary is narrow:

1. The acceptor remains responsible for verifying purchases and producing
   evidence. This repository does not inspect Steam items, query a marketplace,
   decide whether an item is wrong, dispute a purchase, cancel a trade, or
   reprice an order.
2. A receipt is only input evidence. The consumer accepts schema
   `souvenir_rejection_receipt/1` and validates its shape, clock, evidence
   envelope, mismatch verdict, exact target identity, and 72-hour hold fields.
3. Only `confirmed_mismatch` can create a hold candidate. `manual_review` rows
   are durable evidence but must have `hold: null` and never block recreation.
   A confirmed mismatch without a proven native target may also carry
   `hold: null`; it is evidence only and never blocks recreation.
4. A hold candidate is valid only for `synthetic_recreate_hold`, exactly 72
   hours, `order_target_only` scope, and `expires_at` exactly equal to
   `detected_at + 72h`.
5. The hold key is exact and reason-bound in the emitted evidence:
   `(marketplace, account, target_key, expression_sha256)`. A differing
   marketplace, account, target key, expression digest, or trailing character
   is unrelated and is not blocked.
6. The consumer is disabled. `evaluate_recreation` returns `decision: "hold"`
   or `"allow"` as dry-run evidence, but always returns `applied: false` and
   `consumer_state: "disabled"`.

## What stays isolated

The following remain outside this repository and outside this consumer:

- inspect-link retrieval or decoding;
- confirmation that a sticker is missing or damaged;
- marketplace reads, writes, retries, cancellation, repricing, or recreation;
- economic decisions such as whether remaining quantity should pause;
- account-specific target registry publication;
- production wiring into the encrypted monitor;
- owner approval and release-control changes.

The primary acceptor evidence verifies which purchased asset arrived and
whether the purchase ledger is internally consistent. It does not by itself
prove souvenir sticker condition. The consumer therefore treats every receipt
as unverified input until it matches the complete contract, and it never
widens acceptance evidence into a mutation.

## Durable replay rules

`ReceiptStore` is an append-only local JSONL ledger:

- missing ledger: zero receipts;
- exact repeat of the same `receipt_id` and payload: idempotent no-op;
- same `receipt_id` with a different payload: fail closed as
  `conflicting_duplicate_receipt`;
- malformed JSON, a non-object row, invalid UTF-8, or a blank row: fail closed;
- restart: reload the ledger and derive the same hold without extending its
  expiry;
- expiry: a target is held strictly before `expires_at`; exactly at expiry the
  decision is `allow`.

The pure evaluation path deduplicates repeated receipts in memory as well, so
replaying a fixture does not create duplicate decisions. The schema is closed:
unexpected top-level, target, evidence, or hold fields are rejected rather than
persisted. The producer's controlled lowercase `reason` evidence field is the
only optional top-level extension currently accepted.

## Operator dry-run

No network or credentials are required. Example using only the anonymous
fixture:

```bash
python tools/rejection_receipt_dry_run.py \
  --ledger /tmp/anonymous-receipts.jsonl \
  --receipt-json tests/fixtures/rejection_receipts/active_confirmed.json \
  --marketplace csfloat \
  --account anonymous-account \
  --target-key anonymous-target-a \
  --expression-sha256 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --at 2026-11-27T00:00:00Z
```

Expected result: `decision` is `hold`, `applied` is `false`,
`consumer_state` is `disabled`, and `/tmp/anonymous-receipts.jsonl` is not
created or modified. Replace the fixture with `expired_confirmed.json` and use
an evaluation time after `2020-01-04T00:00:00Z` to verify an expired receipt
returns `allow`.

## Publish and integration checklist

A separate disabled integration **is required**. This PR intentionally does
not wire the consumer into the encrypted monitor because that would change
runtime behavior before the cross-repository producer contract and owner
release decision exist.

Before any production integration:

1. Eric approves the final schema, including receipt ID generation, mismatch
   taxonomy, clock-skew tolerance, and whether receipt state changes from
   `proposed_not_live` to `active` before enablement.
2. The producer publishes a durable target registry before fills, mapping a
   buy-order ID to exactly one `(account, target_key, expression_sha256)`.
   Ambiguous or missing mappings produce no hold; whether they remain
   `manual_review` or targetless `confirmed_mismatch` evidence must be fixed
   in the final cross-repository schema decision.
3. The producer emits anonymous-safe internal receipts with complete evidence
   digests; no credentials, cookies, wallet data, trade URLs, or strategy
   values are written.
4. A decrypted monitor integration reads the local ledger and calls the pure
   decision function immediately before recreation only. It must not cancel or
   reprice active orders and must not alter write caps, rates, prices, or
   budgets.
5. That integration remains behind the current release controls, with a
   disabled default, and is validated first against generated fixtures and a
   decrypted offline end-to-end harness.
6. Operator verification uses the dry-run command above, then compares the
   printed exact identity, receipt IDs, reasons, expiry, `applied: false`, and
   unchanged ledger before considering any enablement.

Until those steps are complete, the correct production behavior is no behavior
change.
