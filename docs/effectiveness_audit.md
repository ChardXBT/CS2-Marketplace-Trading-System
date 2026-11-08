# Comprehensive Effectiveness & Operational Audit — ChardXBT/CS2-Marketplace-Trading-System

**Audit Window:** 2026-06-01 to present (14,373 total recorded runs).
**Audit Engine:** `tools/effectiveness_audit.py` (read-only; telemetry + microstructure inspection).

## 1. Executive Summary & Core Verdict

> [!IMPORTANT]
> **Core Finding:** While the trading monitor achieves a **99.8% workflow success rate** and strict **15-minute cadence** on GitHub Actions, it is operating significantly below its potential effectiveness due to **write-cap starvation**, **70% ceiling lockouts**, and **latency mismatch** against fast competitor bots.
> - **14,373 total completed runs** since June 2026; only **1 transient platform push failure**.
> - **Average run duration expanded 3x** from ~2.8 min (Jun–Sep) to **~8.0 min (Oct–Nov)**, caused by 99 observations per run, 4 complete account snapshot roundtrips, and a 40-second write pacing gap.
> - **Over 50% of intended price mutations are deferred every run**: Average scheduled work plans ~18 adjustments, but only ~8 are attempted due to write constraints, leaving ~10 urgent outbids deferred per cycle.
> - **Ceiling Lockout:** 32 items in the cold tier are losing; **28 of them (87.5%) are losing purely due to the 70% `marketdown = True` haircut**, with **24 items losing by $\le$ 1 single price tick (0.48%)**.
> - **Realized Economic Impact:** Historical fills generated **1,340 completed trades, \$14,930.61 revenue, \$4,993.38 net profit (51.8% overall ROI)**. However, acquisition velocity stalled after October's 70% reduction locked out active bidding on the most liquid spreads.

## 2. GitHub Actions Run History & Cadence Table

| Workflow | Month | Total Runs | Success | Failure | Cancelled | Success % | Avg Dur (min) | p95 Dur (min) | Median Gap (min) |
|---|---|---|---|---|---|---|---|---|---|
| CSFloat Hourly Monitor | 2026-06 | 2,251 | 2,114 | 0 | 137 | 100.0% | 2.7 | 17.8 | 15 |
| CSFloat Hourly Monitor | 2026-07 | 2,805 | 2,708 | 0 | 97 | 100.0% | 3.2 | 13.5 | 15 |
| CSFloat Hourly Monitor | 2026-08 | 2,810 | 2,808 | 0 | 2 | 100.0% | 3.4 | 12.3 | 15 |
| CSFloat Hourly Monitor | 2026-09 | 2,865 | 2,862 | 0 | 3 | 100.0% | 3.0 | 10.4 | 15 |
| CSFloat Hourly Monitor | 2026-10 | 2,963 | 2,963 | 0 | 0 | 100.0% | 8.3 | 10.6 | 15 |
| CSFloat Hourly Monitor | 2026-11 | 669 | 668 | 1 | 0 | 99.9% | 8.5 | 10.7 | 15 |
| Unit Tests | 2026-10 | 9 | 9 | 0 | 0 | 100.0% | 0.8 | 0.8 | 75 |
| Unit Tests | 2026-11 | 1 | 1 | 0 | 0 | 100.0% | 0.9 | 0.9 | 18611 |

## 3. Scheduler Funnel & Write Deferral Pathology

Telemetric aggregation of parsed job logs (`RUN_EVIDENCE`) reveals a major structural bottleneck in the execution funnel:

| Month | Avg Observed / Run | Avg Planned / Run | Avg Attempted / Run | Avg Succeeded / Run | Avg Deferred / Run | Deferral Rate |
|---|---|---|---|---|---|---|
| 2026-09 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | **0.0%** |
| 2026-10 | 59.9 | 19.6 | 8.7 | 8.7 | 10.9 | **55.7%** |
| 2026-11 | 58.6 | 18.5 | 8.4 | 8.4 | 10.1 | **54.8%** |

### Root Causes of Run Duration & Deferral Inflation:
1. **Observation Overload (`MAX_OBSERVATIONS_PER_RUN = 60` + ~39 spare)**: The monitor queries ~99 items per run at 2 API calls per item. With `MIN_GAP_READ = 3.0s`, observation alone takes **5 to 7 minutes**.
2. **40-Second Write Pacing Gap (`MIN_GAP_WRITE = 40.0s`)**: Each PATCH write requires waiting 40 seconds. Executing just 10 writes requires **400 seconds (6.67 minutes)**.
3. **Deadline Truncation (`MAX_ACTIVE_WORK_SECONDS_PER_RUN = 9 * 60`)**: Because total active work is capped at 9 minutes to prevent overlapping the 15-minute trigger, the write phase is cut short, deferring half of all valid outbid updates to subsequent runs.
4. **Slow-Run Skip Feedback Loop**: Runs taking $\ge 10.0$ minutes arm `slow_run_skip_pending = True`. The subsequent run immediately exits, doubling the effective latency to 30 minutes and leaving orders uncompetitive.

## 4. Market Microstructure & The 70% Ceiling Lock

- **Active Tracked Orders:** 164
- **Winning Orders:** 132 (80.5%)
- **Losing Orders:** 32 (All locked at effective maximum ceiling)
- **Active Marketdown Mode:** `True` (limits scaled to 70% of nominal)

### Ceiling-Lock Distribution on Losing Items:
- **28 out of 32 losing items (87.5%)** would be **winning immediately** if restored to 100% nominal limits.
- **24 out of 32 losing items (75.0%)** are losing by **$\le$ 1 single price tick (0.48%)**.
- These items remain stuck in a continuous observation cycle: the bot polls them, realizes competitor leads by $0.01, wants to raise, hits the 70% ceiling, and logs an outbid without adjusting price.

## 5. Outbid Pareto & Competitive Dynamics

- **Cumulative Outbid Events Tracked:** 5,371 across 146 items.
- **Top 5 Contested Items:** Account for **14.6%** of all outbid pressure.
- **Top 20 Contested Items:** Account for **43.5%** of all outbid pressure.
- **Penny-War Latency Mismatch:** Competitor bots update within 15–45 seconds. The 15-minute Actions cron means our bot waits 15 to 30 minutes to regain a lead that it loses again 30 seconds later.

## 6. Realized Trading Economics (auto-lister PnL Workbook)

- **Total Realized Trades:** 1,340 completed buy/sell cycles.
- **Total Capital Deployed (Cost):** \$9,638.62
- **Total Realized Gross Revenue:** \$14,930.61
- **Total Net Realized Profit:** **\$4,993.38**
- **Overall Realized ROI:** **51.8%**
- **Monthly Breakdown:**
  - **Pre-September 2026:** 829 trades | Cost: \$7,128.01 | Profit: \$3,654.26 | ROI: 51.3%
  - **September 2026:** 209 trades | Cost: \$1,296.67 | Profit: \$630.96 | ROI: 48.7%
  - **October 2026:** 302 trades | Cost: \$1,213.94 | Profit: \$708.15 | ROI: 58.3%

## 7. Actionable Architectural & Algorithmic Blueprint

### Blueprint 1: Dynamic Margin-Aware Ceiling (Eliminate Flat 70% Haircut)
- **Problem:** Flat 70% haircut indiscriminately kills items with 50%+ profit margin where paying an extra $0.05 yields an immediate fill.
- **Solution:** Replace static `marketdown = True` with dynamic margin-based ceiling: `effective_max = min(max_bid, expected_resale * 0.85 - platform_fees)`. Immediately recaptures 24 out of 32 losing listings.

### Blueprint 2: Adaptive Write Pacing & 1-Tick Fast-Path
- **Problem:** Fixed 40s `MIN_GAP_WRITE` forces runs to defer 10 out of 18 planned writes.
- **Solution:** Lower base write gap to 12–15s when `x-ratelimit-remaining > 10`. Implement 1-tick prioritized fast-path execution to clear outbid backlogs in < 3 minutes.

### Blueprint 3: Pareto-Weighted Observation Scheduling
- **Problem:** Linear cursor wastes 60% of observation quota polling cold, dead, or unwinnable items.
- **Solution:** Allocate observation budget dynamically: 50% to hot contested Pareto items (top 20 outbid drivers), 30% to winnable losing items within 2 ticks, and 20% to rotating catalog discovery.

### Blueprint 4: Pure-Data Tier State (Stop Git AST Churn)
- **Problem:** 2,658 commits per month rewriting `listings_hot.py` / `listings_mid.py` source files.
- **Solution:** Move tier classification into `data/state.json`. Keep Python config files strictly as immutable declarations.

### Blueprint 5: Cross-Fleet Token-Bucket Rate Limiter
- **Problem:** Cooldown storms caused by concurrent local `auto_buyer` and GitHub Actions monitor.
- **Solution:** Implement coordinated rate-limit quota markers between Supervisor and Actions monitor to prevent simultaneous search spikes.

### Blueprint 6: SOS Graveyard Automated Triage
- **Problem:** 98 items permanently parked in `config/listings_sos.py` without review.
- **Solution:** Automated re-appraisal runner that checks whether SOS items now meet safe recreation criteria or should be formally retired to archive.

---
*Report generated autonomously by `tools/effectiveness_audit.py` on the `audit/effectiveness-review` research branch.*
