# Public bid-sync receipt compatibility contract

This document defines `bid-sync-receipt/1`, an anonymous metadata receipt for
compatibility testing around the monitor's published bid-sync artifacts. The
contract is a consumer contract only. It does not publish anything, change the
workflow, alter deployment, scheduling or permissions, inspect a marketplace,
or enable any trading behavior.

The fixtures and reference validator in this repository use generated values
only. A receipt carries counts, public workflow identity, repository artifact
paths, published byte sizes, and SHA-256 digests. It must not carry item
names, sticker or variant labels, prices, thresholds, account identifiers, marketplace order or
listing identifiers, trade identifiers, wallet data, cookies, credentials, or
strategy text.

This contract is independent from the disabled souvenir rejection-receipt
consumer. It does not consume `souvenir_rejection_receipt/1`, does not create
or evaluate a recreation hold, and does not change that consumer's owner-held
integration boundary.

## Existing artifact inventory

The monitor workflow currently publishes runtime evidence by checkpointing the
repository. In this document "published artifact" means a file published by
that workflow checkpoint, not a GitHub Actions artifact upload; the repository
currently has zero Actions artifact uploads.

| Artifact | Publication shape | Content schema evidence | Receipt treatment |
| --- | --- | --- | --- |
| `data/order_book/YYYY/MM/DD/<run_id>-<attempt>.jsonl.gz` | One immutable gzip-compressed JSONL capture per workflow run/attempt. Git history shows current run files using this exact shape, for example a numeric run ID followed by `-1.jsonl.gz`. | The merged PR #18 description and `docs/ORDER_BOOK.md` are the primary sources. The PR states that each book record carries observation time, source, listing identity fields, item/query context, row limit and competitor price; row entries carry order row metadata such as `id`, `price`, `qty`, `expression` or `hybrid_properties`, `market_hash_name`, `created_at`, and whether the row matched one of the run's own orders; a trailer records overflow beyond the 5,000-book cap. The document and live capture blobs are git-crypt ciphertext in a Free worker checkout, so their plaintext could not be independently reread here. | Referenced only as an opaque object with `kind: order_book_jsonl_gz`, exact path, byte count and SHA-256 digest. The receipt never copies row content. |
| `data/active_bid_prices.json` | Mutable exported snapshot committed by the bid-sync checkpoint. | The downstream `cs-skins-manager` README is the current readable consumer documentation: the JSON file is a top-level array of rows with a non-empty `label` and positive finite numeric `threshold_usd`. A stored consumer-side copy independently has exactly those two keys per row. The producer's current blob is git-crypt ciphertext here, so row values were not inspected. | Referenced only as an opaque snapshot object with fixed path, byte count and SHA-256 digest. Labels and thresholds are forbidden in receipts. |
| `data/active_bid_prices.csv` | Mutable exported snapshot committed beside the JSON export. | The file name and path are confirmed in git history. Its current bytes are git-crypt ciphertext in this checkout, and no readable consumer contract for the CSV columns was found. Its exact column schema is therefore a labeled gap, not an inference. | Referenced only as an opaque snapshot object with fixed path, byte count and SHA-256 digest. Consumers must not invent column compatibility from this receipt. |
| `data/state.json`, `data/outbid_stats.json`, tier configuration and similar checkpointed runtime state | Rewritten operational state. | These are runtime state/configuration inputs, not stable public compatibility artifacts for this receipt. | Out of scope. Receipts must not embed their contents. |

The order-book fields above are compatibility provenance, not permission to
publish those fields in a receipt. The public receipt schema is deliberately
narrower than the encrypted artifact schemas.

## Receipt envelope

A receipt is one UTF-8 JSON object. The schema is closed: unknown fields are
errors rather than ignored extensions. Unknown schema versions fail closed.

Required top-level fields, with no additional fields:

```json
{
  "schema_version": "bid-sync-receipt/1",
  "receipt_id": "bid-sync-4242424242-1",
  "run": {},
  "scan": {},
  "updates": {},
  "artifact_versions": {},
  "artifacts": []
}
```

### `run`

Required fields, with no additional fields:

- `repository`: exactly `ChardXBT/CS2-Marketplace-Trading-System`.
- `workflow`: exactly `CSFloat Hourly Monitor`.
- `run_id`: the decimal GitHub Actions run ID as a string, positive, no leading zero, at most 20 digits.
- `attempt`: positive JSON integer no greater than `2^53 - 1`. Booleans are not integers.
- `started_at` and `completed_at`: RFC3339 UTC timestamps with seconds and a literal `Z`, for example `2026-01-02T03:04:05Z`. Offsets and fractional/local forms are outside v1. `completed_at` must not precede `started_at`.

`receipt_id` must exactly be `bid-sync-<run_id>-<attempt>`. Consumers key
idempotence by `(run_id, attempt)`, not by artifact path alone.

### `scan`

Required fields, with no additional fields:

- `status`: `complete` or `partial`.
- `pages_completed`: non-negative JSON integer no greater than `2^53 - 1`.
- `terminal_page_complete`: boolean.
- `rows_observed`: non-negative JSON integer no greater than `2^53 - 1`.
- `incomplete_reason`: `null` or a non-empty string without control characters and with at most 4,096 characters.

A complete scan requires `terminal_page_complete: true` and
`incomplete_reason: null`. A partial scan is valid evidence, but consumers must
not treat it as a complete scan. A partial scan must carry either
`terminal_page_complete: false`, a non-empty `incomplete_reason`, or both. The
synthetic partial fixture uses reason `partial_page`; the enum of reasons is not
closed in v1 so a producer can distinguish later causes without changing the
envelope.

### `updates`

Required fields, with no additional fields:

- `candidates_observed`: non-negative JSON integer no greater than `2^53 - 1`.
- `eligible_updates`: non-negative JSON integer no greater than `2^53 - 1`, never greater than `candidates_observed`.
- `no_eligible_updates`: boolean exactly equal to `eligible_updates == 0`.

`no_eligible_updates` is a valid complete-scan outcome. It is not a scan
failure and does not make a receipt partial.

### `artifact_versions`

Required object with exactly these keys and values:

```json
{
  "order_book_jsonl_gz": "order-book-jsonl/1",
  "active_bid_prices_json": "active-bid-prices-json/1",
  "active_bid_prices_csv": "active-bid-prices-csv/1"
}
```

These versions describe compatibility expectations for the referenced artifact
families. They do not decrypt or validate artifact contents. For the CSV
snapshot, v1 means byte-level descriptor compatibility only; it does not assert
the still-unverified column schema. Any unknown or changed value is an error
for a v1 consumer.

### `artifacts`

Required array. It may be empty only for partial-scan evidence. A complete
sync must describe at least the two fixed snapshot outputs,
`active_bid_prices_json` and `active_bid_prices_csv`; a receipt without those
output descriptors is not proof of a successful sync, even when there were no
eligible updates. The order-book capture remains optional because the producer
has an owner-controlled capture switch. Each entry is a closed object with exactly:

- `kind`: one of `order_book_jsonl_gz`, `active_bid_prices_json`, or `active_bid_prices_csv`.
- `path`: repository-relative POSIX path.
- `sha256`: 64 lowercase hexadecimal characters.
- `bytes`: non-negative JSON integer no greater than `2^53 - 1`.

Each kind and each path may appear at most once. Path rules:

- `active_bid_prices_json` must use exactly `data/active_bid_prices.json`.
- `active_bid_prices_csv` must use exactly `data/active_bid_prices.csv`.
- `order_book_jsonl_gz` must match `data/order_book/YYYY/MM/DD/<run_id>-<attempt>.jsonl.gz`, and the run ID and attempt in the path must exactly match the envelope. The date directory must be a valid calendar date. It is producer evidence; v1 does not require it to equal either timestamp because a run can cross midnight.

`sha256` and `bytes` describe the exact published repository blob bytes at the
named path. For a git-crypt encrypted artifact this means the committed
ciphertext bytes, not decrypted plaintext. A SHA-256 digest is a compatibility
fingerprint, not proof of correctness or safety. Consumers must verify digest
and byte count before treating a fetched artifact blob as the object named by
the receipt; decrypted content needs a separate content validator outside this
contract.

## Duplicate and unknown-version compatibility

Consumers evaluate receipts as an append-only stream:

1. A first valid `(run_id, attempt)` is accepted.
2. A semantically identical repeat after canonical JSON object ordering is an idempotent duplicate and is not counted again.
3. The same `(run_id, attempt)` with any payload difference is a conflicting duplicate. The conflict fails closed and the later payload is not accepted.
4. A different attempt is a different run even when `run_id` is the same.
5. `schema_version` other than `bid-sync-receipt/1` is `unsupported_schema_version`. A consumer must not inspect unknown-version fields, infer meaning from them, or treat the receipt as partial evidence.
6. Invalid JSON, duplicate JSON object keys, missing fields, nulls in required typed positions, booleans in integer positions, negative values, values above the JSON safe integer limit, unexpected fields, duplicate artifact paths, mismatched artifact identities, missing complete-scan output descriptors and non-UTC timestamps all fail closed.

Canonical comparison for exact duplicates uses JSON object keys sorted
recursively, UTF-8, no insignificant whitespace, and the shortest non-ASCII
escaped form produced by the reference implementation. Consumers may instead
compare a hash of the canonical form.

## Reference verification

The reference implementation is `src/bid_sync_receipts/contract.py`; its
`loads_receipt_json` helper rejects duplicate JSON object keys while parsing.
The read-only operator entry point is:

```bash
python tools/bid_sync_receipt_contract.py \
  tests/fixtures/bid_sync_receipts/complete_scan.json \
  tests/fixtures/bid_sync_receipts/partial_page.json \
  tests/fixtures/bid_sync_receipts/no_eligible_updates.json
```

The command prints one JSON compatibility summary. It reads only the supplied
files, writes nothing, and uses no network or credentials. Including
`unknown_version.json` must make the command exit nonzero with
`unsupported_schema_version`.

The synthetic fixture matrix covers:

- complete scan with all three artifact descriptors;
- partial page, valid as partial evidence but never complete;
- complete scan with no eligible updates;
- exact duplicate run receipt as idempotent;
- conflicting duplicate run receipt as fail-closed;
- unknown receipt version as fail-closed;
- empty artifact lists on partial evidence, missing output descriptors on complete evidence, malformed and deeply nested JSON, duplicate JSON object keys, missing and null fields, wrong types, zero, negative and huge integers, unicode and empty identity strings, out-of-order duplicate ingestion, timezone offsets, artifact path/run mismatch, duplicate artifact paths, uppercase digests, and closed-schema extra fields.


## Offline workflow-run evidence validation

`src/bid_sync_receipts/workflow_evidence.py` provides a second pure validation
layer for archived or mocked GitHub Actions responses. It does not call the
GitHub API. It accepts a workflow-run object, the corresponding jobs response,
and a `bid-sync-receipt/1` receipt, then fails closed unless all of the
following agree:

- the run is `CSFloat Hourly Monitor`, completed successfully, and has the
  same run ID and retry attempt as the receipt;
- exactly one job belongs to that run and completed successfully;
- the job has executed steps, so a zero-step runner provisioning or billing
  failure can never be read as a successful sync;
- `Run the monitor script` and `Checkpoint runtime changes` are both present,
  ordered, completed and successful;
- the receipt is valid complete-scan evidence with the required snapshot
  output descriptors.

A green top-level workflow conclusion by itself is not accepted as proof of a
sync. Retried runs are valid only when the receipt attempt matches the Actions
`run_attempt`. Missing, duplicate, cancelled, timed-out, skipped, failed or
mismatched evidence fails closed. The read-only operator entry point is:

```bash
python tools/bid_sync_workflow_evidence.py run.json jobs.json receipt.json
```

All three files must be local, already-retrieved JSON. The command writes
nothing and performs no network access.

## Remaining public-schema gap and falsifiable next experiment

The exact plaintext column schema of `data/active_bid_prices.csv` remains
unverified in the public/locked checkout. Do not infer it from the JSON schema.
A falsifiable owner-side experiment is: in a decrypted test-only checkout, read
only the CSV header and compare it with the JSON row keys; report the header
names without row values. If the header differs from the JSON row keys, the
CSV artifact version must be incremented before any consumer relies on column
compatibility.

The plaintext `docs/ORDER_BOOK.md` and order-book blobs are likewise encrypted
in the locked checkout. The artifact schema summary above is cross-checked
against merged PR #18's public description and observable checkpoint path
names. A falsifiable owner-side experiment is to validate one synthetic
decrypted order-book file against that documented record/trailer shape while
redacting all item, price and order values before publication.

No owner action is required for this contract test itself. Owner approval is
required before any producer emits real receipts, before any workflow or
checkpoint change, and before any consumer uses real artifact contents.
