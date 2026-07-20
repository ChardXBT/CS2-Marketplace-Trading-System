# CS2 Marketplace Trading System

CS2 Marketplace Trading System is a modular automation platform for maintaining
marketplace buy orders, monitoring configured opportunities, publishing updated
target definitions, and managing inventory after acquisition.

This repository contains the core buy-order monitor. The broader system also
includes focused companion services for auto-listing, marketplace discovery,
and target-file synchronization. Each service owns a narrow responsibility and
produces durable run evidence so failures remain isolated and diagnosable.

## System overview

```mermaid
flowchart LR
    A["Target Definition Sync"] --> B["Validated Target Configuration"]
    B --> C["Buy-Order Monitor"]
    B --> D["Marketplace Watchers"]
    C --> E["Marketplace Orders"]
    D --> F["Ranked Opportunity Alerts"]
    E --> G["Owned Inventory"]
    G --> H["Auto-Lister"]
    H --> I["Sale and Inventory Records"]

    C --> J["Structured State and Run Evidence"]
    D --> J
    H --> J
    A --> J
```

The components exchange validated configuration and structured results rather
than sharing browser sessions or mutable runtime state. A watcher failure
therefore cannot erase order state, and a listing failure cannot interrupt the
buy-order monitor.

## Component map

| Component | Responsibility | Execution model |
| --- | --- | --- |
| Buy-Order Monitor | Reconcile, create, reprice, and retire configured marketplace orders | Scheduled Python workflow |
| Auto-Lister | Reconcile owned inventory, create or renew listings, and confirm sales conservatively | Scheduled browser/API workflow |
| Skinport + CS.Money Finder | Scan configured targets, normalize results, and publish ranked alerts | Daily two-stage browser workflow |
| Steam Market Watcher | Evaluate additional marketplace opportunities with currency and condition normalization | Scheduled workflow |
| BUFF Watcher | Optional source adapter retained behind an explicit enable/disable control | Disabled by default |
| Target Definition Sync | Validate and publish the target files consumed by the other components | Daily configuration workflow |

## 1. Core buy-order monitor

The monitor maintains a bounded set of configured targets. Each run loads
durable state, reconciles it against authoritative live orders, selects the
appropriate priority tier, and decides whether an order should be left
unchanged, adjusted, recreated, or retired.

```mermaid
flowchart TD
    A["Scheduled trigger"] --> B["Load configuration and durable state"]
    B --> C["Fetch live orders"]
    C --> D["Startup reconciliation"]
    D --> E["Select due priority tiers"]
    E --> F["Observe every due target"]
    F --> G{"Validated current-run snapshot"}
    G -->|Missing| H["Validate recreate eligibility"]
    G -->|Competitive| I["Keep or reduce excess exposure"]
    G -->|Outbid| J["Plan bounded adjustment"]
    G -->|Ambiguous| K["Preserve state and defer"]
    H --> R["Rank one candidate per target"]
    I --> R
    J --> R
    K --> M["No unsafe action"]
    R --> L["Reserve attempt budget and write"]
    L --> N["Verify response and reconcile"]
    M --> O["Persist run summary"]
    N --> O
    O --> P["Atomic state update"]
    P --> Q["Notifications and data sync"]
```

### Bid lifecycle

| Phase | Behavior |
| --- | --- |
| Startup | Load configuration, validate state shape, and fetch authoritative live orders |
| Reconciliation | Match configured targets to live orders while preserving ambiguous or in-flight state |
| Tier selection | Concentrate expensive checks on active targets and rotate quieter targets less frequently |
| Market evaluation | Compare current competition, comparable listings, and target-specific safety bounds |
| Action planning | Prefer the smallest valid update and avoid unnecessary cancel/recreate cycles |
| Write verification | Treat the remote response and later reconciliation as authoritative evidence |
| Persistence | Atomically save state, summaries, cooldowns, and tier movement |
| Finalization | Publish notifications and safely synchronize bot-managed data |

### Tiered scheduling

Targets move among hot, mid, and cold tiers based on recent activity. The cycle
runner gives active targets priority while periodically including quieter
tiers. Pre-move and post-move validation require each target to appear in
exactly one tier, preventing duplicate orders and silent omissions.

| Mode | Purpose |
| --- | --- |
| `cycle` | Normal unattended scheduling across all due tiers |
| `hot` | Active and high-priority targets only |
| `mid` | Medium-frequency targets only |
| `cold` | Low-frequency targets only |

### Observation-first write scheduling

Each due listing is observed before normal order writes begin. The existing bid
strategy produces at most one candidate from that current-run snapshot, then a
global queue ranks safety corrections, temporary-main restoration, actionable
outbids, missing-order repair, quantity repair, and exposure-reducing decreases.
Decrease priority uses `(current price - target price) * quantity`, matching the
capital reserved by a buy order.

The default run has no artificial write-count ceiling. Candidates execute in
priority order until work is exhausted, their market snapshots become stale, or
a write-side HTTP 429 trips the run-local circuit breaker. The circuit breaker
checkpoints state and defers all remaining mutations to the next scheduled run;
read/search exhaustion retains the longer persisted cooldown. Failed requests
are counted, and one listing cannot write twice in a run. Deferred metadata and
the observation cursor live under `bid_scheduler` in `data/state.json`;
persisted entries affect fairness only and are never executed without a fresh
valid observation.

An ambiguous PATCH/POST/DELETE transport timeout also checkpoints and stops the
run because the remote outcome cannot be known safely. The next complete order
snapshot reconciles that outcome before any retry. The 80-minute watchdog
checkpoints state and pre-arms a one-run skip before the workflow's 85-minute
hard timeout.

GitHub Actions runs are serialized and queued dispatches check out the latest
`master` rather than their captured dispatch SHA. Runtime checkpoints retry
disjoint concurrent pushes, but never merge overlapping encrypted files. A
rare overlap is preserved on a `runtime-recovery/<run>` branch and reported in
the job summary and Discord log so the next scheduled run can reconcile safely.

Tune these controls in `src/market_monitor/strategy_settings.py`:

- `MAX_WRITE_OPERATIONS_PER_RUN` (`0` disables the artificial count ceiling)
- `RESERVED_RESTORATION_SAFETY_WRITES` (used only with a finite ceiling)
- `MAX_OBSERVATIONS_PER_RUN` (`0` observes every due listing)
- `MAX_QUEUE_DEFERRAL_RUNS`
- `MAX_MUTATION_SNAPSHOT_AGE_SECONDS`

### Temporary extra bids

Temporary extras are declared in encrypted `config/temporary_bids.py`. Add
`"temporary_suspend": True` to the paired main listing while leaving its exact
recreation policy, price, quantity, and identity fields intact. A temporary
entry must carry `"temporary_extra": True`; use `main_tracking_key` when the
pair should not be inferred from the initial one-to-one config order.

The monitor adopts a blank temporary order ID only when a complete paginated
active-order sync finds one unique, strongly constrained semantic match. It
then durably records the pair, suspends the main through the shared write
budget, and leaves the main tag in place. Once a later complete sync proves the
temporary order is gone, restoration recreates the saved main subject to the
current floor, cap, tick, quantity, hybrid-property, and active-order-count
guards. Ambiguity, incomplete reads, count mismatches, missing tags, or policy
failures preserve state and block destructive action.

Temporary orders never participate in repricing, recreation, quantity top-up,
tiering, cleanup, or scraper exports. The canonical main listing remains in the
normal tier file, so downstream `active_bid_prices.json` consumers retain its
last verified/configured threshold throughout suspension.

### Market-aware protection

The monitor can compare valid buy-now listings with the current order plan and
temporarily tighten exposure when the visible market changes. Missing,
incomplete, or invalid comparison data fails closed rather than authorizing a
wider bid. An ambiguous write outcome is preserved for reconciliation instead
of being blindly retried.

## 2. Auto-lister

The auto-lister takes over after acquisition. It compares configured inventory
with authoritative active-listing and inventory responses, then creates,
maintains, renews, or retires listings conservatively.

```mermaid
flowchart TD
    A["Run starts"] --> B["Load definitions and durable state"]
    B --> C["Fetch active listings and inventory"]
    C --> D{"Inputs authoritative?"}
    D -->|No| E["Record failure; preserve prior state"]
    D -->|Yes| F["Reconcile each configured item"]
    F --> G{"Observed state"}
    G -->|Already active| H["Record current listing"]
    G -->|Expired| I["Apply renewal policy"]
    G -->|Available and unlisted| J["Create listing"]
    G -->|Repeatedly unavailable| K["Confirm sold state"]
    H --> L["Persist verified result"]
    I --> L
    J --> L
    K --> L
```

The lister never infers a sale from a generic API or browser failure. Missing
inventory requires repeated explicit evidence, while failed or incomplete
reads leave the previous state intact. Bot-managed files are written
atomically and synchronized only after the run reaches a verified terminal
state.

## 3. Marketplace watchers

The watcher layer searches several CS marketplaces for configured
opportunities. Although each source adapter differs, the services share one
high-level contract:

```text
load targets -> fetch source -> normalize currency and item identity
             -> validate source-specific requirements -> rank results
             -> publish alerts -> write structured completion evidence
```

### Skinport and CS.Money finder

The daily finder contains two independently runnable stages. It can scan both
sources together or resume only the unfinished stage after an interruption.
Its completion artifact records the selected mode, completed checks,
authentication health, source-level errors, alert counts, and final status.

This split-stage design prevents a late failure from repeating already
completed browser work and makes partial recovery explicit rather than
guessing from the last log line.

### Steam market watcher

This workflow normalizes currency, evaluates configured item and condition
requirements, ranks qualifying results, and publishes a compact summary. Its
state and failures are isolated from order maintenance, so an alerting outage
does not alter active bids.

### BUFF watcher

The BUFF adapter is retained as an optional integration but is disabled by
default. Disabled means the workflow is not inspected, launched, or included
as a completion dependency until it is explicitly enabled.

## 4. Target definition sync

Target configuration changes independently from runtime code. A dedicated
daily workflow validates source definitions and publishes deterministic target
files consumed by the buy-order monitor and watchers.

The sync process:

1. reads the current source definitions;
2. validates required fields, types, uniqueness, and supported names;
3. generates deterministic target configuration;
4. verifies the generated files before replacement;
5. atomically publishes the update;
6. records a timestamped completion result;
7. synchronizes changes only after validation succeeds.

Separating definition maintenance from transaction processes reduces the risk
that a malformed target update reaches a live workflow.

## Reliability model

| Failure condition | System response |
| --- | --- |
| Missing or malformed configuration | Reject the run before marketplace writes |
| Incomplete source response | Preserve prior state and record a failed/incomplete result |
| Unknown order or listing outcome | Keep the item uncertain and refuse a duplicate submission |
| Authentication failure | Record an actionable failure without claiming successful work |
| Interrupted multi-stage scan | Preserve completed-stage evidence and resume only unfinished work |
| Missing or corrupt state | Restore a validated backup when available; otherwise fail closed |
| Repeated item absence | Require corroborating evidence before recording a sale |
| Optional component disabled | Exclude it completely from execution and completion requirements |

### State, logs, and completion evidence

The system distinguishes three evidence layers:

- **durable runtime state** for reconciliation context, pending work, and last
  known authoritative observations;
- **structured run artifacts** containing explicit status, timestamps, mode,
  counts, and failure classifications;
- **human-readable logs** for detailed decisions and diagnostics.

A process launch or a single log message is not treated as proof of successful
work. Completion is based on a terminal run artifact plus internally
consistent counts and timestamps. Logs remain supporting evidence and make
failures explainable without becoming the only source of truth.

### Persistence safety

- State replacement is atomic where practical.
- Previous known-good snapshots are retained for recovery.
- Generated configuration is validated before publication.
- Ambiguous remote writes remain pending until reconciliation.
- Data synchronization occurs only after a completed run boundary.
- Runtime secrets and account-specific values remain outside public source.

## Technology

- Python for orchestration, state transitions, data validation, APIs, and
  testing;
- JavaScript/Node.js for selected browser-driven workflows;
- Playwright and Selenium for isolated browser adapters;
- REST APIs for orders, listings, pricing, and inventory;
- GitHub Actions and controlled local launchers for scheduled execution;
- structured JSON/JSONL artifacts and atomic files for run history;
- Git-based configuration and validated data synchronization.

## Repository layout

The checked-in code in this repository is the core buy-order monitor and its
supporting utilities.

```text
.
├── .github/                         # Scheduled and manual workflows
├── config/
│   ├── listings.py                  # Primary configured targets
│   ├── listings_hot.py              # Active tier
│   ├── listings_mid.py              # Medium-frequency tier
│   └── manual_bids.py               # Explicit manual entries
├── data/
│   ├── state.json                   # Durable monitor state
│   └── outbid_stats.json            # Competitive-pressure history
├── sandbox/
│   ├── runner.py
│   └── test_listings.py
├── src/market_monitor/
│   ├── monitor.py                   # Main orchestration
│   ├── monitor_market.py            # Market reads and comparison
│   ├── monitor_processing.py        # Per-target decisions
│   ├── monitor_runtime.py           # Runtime and watchdog boundaries
│   ├── monitor_tiers.py             # Tier movement and validation
│   ├── listing_loader.py            # Configuration loading and normalization
│   └── strategy_settings.py         # Central runtime controls
├── export_bid_prices.py
├── show_losing_bids.py
├── sync_quantities.py
├── main.py
└── test_file.py
```

## Running the core monitor

Install dependencies in an isolated environment, provide required runtime
configuration through environment variables, and use the stable entrypoint:

```bash
python main.py
```

Run the isolated sandbox entrypoint:

```bash
python test_file.py
```

Generate a read-only losing-bid report from persisted state:

```bash
python show_losing_bids.py
python show_losing_bids.py --tier hot
python show_losing_bids.py --tier mid
python show_losing_bids.py --tier cold
```

The report performs no marketplace writes and does not modify state or
configuration.

## Security and public scope

Credentials are supplied through runtime secrets, not source files. Sensitive
configuration remains outside public documentation and is protected by the
repository's existing encrypted/configuration workflow. This README describes
system boundaries and safety properties without publishing account details,
private identifiers, bid values, or strategy thresholds.

The design prioritizes recoverability, bounded risk, failure isolation, and an
auditable explanation for every automated decision.
