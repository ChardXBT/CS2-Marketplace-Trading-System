# Reversible market limits

`config/market_state.py` contains the one operator switch:

```python
marketdown = True
```

With `True`, the monitor sets each saved active minimum and maximum to 70% of
the original limit, rounded **up** to cents with decimal arithmetic. With
`False`, it restores the exact original minimum and maximum at the next startup.
Changing the switch does not restore historical order IDs, quantities, live
bids, or lifecycle state. Normal bidding decides subsequent increases.

`config/market_snapshot.json` saves the original active rows, full config file
contents, capture time, and source commit. Git encrypts this file. Startup never
overwrites or silently recreates it. Repeated runs and switch cycles always use
this baseline. Tier moves and changed buy-order IDs do not change a row's
semantic identity. Original archived and SOS rows stay unchanged. An originally
active row moved to SOS retains its policy so later restoration is consistent.

Active temporary orders are included in the baseline. Manual order ID protection
lists contain no min/max values. The switch preserves separate temporary market
safety ceilings. The effective maximum is always the lowest active ceiling,
including the configured maximum. A safety ceiling above the reduced maximum
cannot raise the effective cap. Disabling marketdown therefore restores nominal
limits without leaving an artificial temporary ceiling from this switch.
Independent market safety changes remain authoritative.

Live bid prices remain truthful while reductions are pending. After a complete,
stable account snapshot and semantic ownership validation, a separate pass
lowers any live managed bid above its active maximum across all tiers. This pass
does not need competitor-book reads. It uses the existing PATCH boundary,
confirmed-success bookkeeping, write budget, rate-limit handling, and freshness
limit. Failed or deferred reductions are found again from the next live snapshot.
The existing price-increment rules can make the actual bid lower than a cents-rounded
maximum; the maximum is never exceeded.

Unknown active identities, damaged snapshots, and invalid switch values stop
startup before any API writes. When adding or changing a row's semantic identity,
review and add its original nominal limits to the snapshot first. Do not derive
a new baseline from already reduced limits. Do not replace the saved baseline
to work around a startup failure.

## Coverage and run evidence

Tier selections have independent durable observation cursors. Small or empty
hot-tier runs cannot rewind cold-tier coverage. Spare observation capacity scans
other active rows through a separate full-catalog cursor. Due listings and the
pending watchlist retain priority. The per-run observation limit stays unchanged,
and only completed observations advance either cursor.
Invalid observations retain existing pending hints and their retry priority.
Valid observations can clear hints that no longer need a write.
The observation phase also stops starting new items after seven minutes. This
reserves time for writes when grouped listings need many market reads.
Active work stops starting new writes after nine minutes, including startup
time, to avoid routinely skipping the next fifteen-minute dispatch. Existing API
pacing is unchanged. If cap reductions remain, the run checkpoints them and
defers normal strategy work. Large reductions can therefore need several runs.

Actions emits non-secret counts for scheduled observations, planned writes,
attempted and successful writes, deferred work, cross-tier spare observations,
cap reductions, and scan-only runs. Detailed account logs remain in the existing
private reporting channel.
Checkpoint retries use bounded five-, ten-, and twenty-second pauses to tolerate
short GitHub server outages while retaining the existing conflict refusal.

Validate locally with `python .github/scripts/validate_runtime_config.py` and the
repository test suite. Neither command sends live CSFloat mutations.
