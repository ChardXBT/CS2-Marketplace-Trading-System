# Research: Market-Price Ground Truth for the "Are We Most Effective?" Audit

**Status:** research complete → execution spec below. Companion to `docs/effectiveness_audit.md` (which audited *run telemetry*). This document covers the missing half: **were our bids priced effectively against the real market, June→November 2026?**

**The question this answers:** for ~thousands of items (164 actively tracked + the full historical universe incl. archived/SOS/branch-split items + every item we filled), what is the cheapest accurate way to get ground-truth market prices for that window — and how should they be joined to our bid log?

---

## 1. Verdict on the CSFloat-only plan (the original hypothesis)

**"Use CSFloat's database" is half right, half wrong.**

| Assumption | Reality |
|---|---|
| "Too slow for thousands of items" | **Wrong for daily data.** `GET /api/v1/history/{market_hash_name}/graph` returns an item's **entire daily price history** (`[{day, avg_price, count}]`, ~1,750 days back to ~2021) in **one request**. 3,000 items ≈ 3,000 requests ≈ ~1–10 h depending on the route's rate limit (see §4). No pagination needed. |
| "May not be accurate" | **Right, with nuance.** `/history/*` records **CSFloat-venue sales only**. That is the venue we trade on (correct for value benchmarks), but volume concentrates in mid/high-tier items — our catalog is overwhelmingly **stickered souvenirs**, many with 0–3 sales/week. Days with `count < ~3` are noise, not ground truth. |
| "CSFloat database" (db.csgofloat.com) | **Wrong tool.** The public float DB tracks float values / paint seeds / ownership observations — it contains **no prices** and no usable dumps. The price data lives in the market API, not the float DB. |
| Per-sale granularity for June–Nov | **Impossible from CSFloat retroactively.** `GET /history/{mhn}/sales` returns only a bounded recent tail (~last tens of sales), no date params, no pagination. For any item with >~50 sales since June, June–Oct individual sales are unreachable via CSFloat. |
| Terms of Service | ⚠️ **Flagged.** CSFloat ToS (12 May 2026) restricts the API to "data relating to your account" and bans automated harvesting. Public routes work keyless, but bulk-pulling 3,000 items is a gray zone — use the account key, honor `X-RateLimit-*`, and accept some account-ban risk or route around it via aggregators. |

---

## 2. The internal gold mine (use this FIRST — zero API calls)

**Git history of `data/state.json` on `master` is already a ~15-minute-resolution time series.** Every monitor run commits the runtime allowlist (`checkpoint_runtime.sh`): `data/state.json`, `data/outbid_stats.json`, `data/active_bid_prices.{json,csv}`, `config/listings{,_hot,_mid,_sos}.py`, etc. **9,000+ commits since June.**

What's inside each commit:

| Field in `state.json` | Meaning | Audit use |
|---|---|---|
| `last_prices[lid]` | **Top competing buy order** (max of up-to-10 orders on the listing's compatible order book, own orders excluded) — ⚠️ NOT the lowest listing/ask | Competitor price series for positioning + outbid analysis |
| `my_bids[lid]` | Our live bid at that run | Our side of the spread |
| `bid_scheduler.*` | Pending writes, `top_competitor`, `winnable_top`, `observed_at`, deferrals | Fills the funnel: what we *meant* to do vs did |
| `temporary_max_bids[lid]` | Safety-cap observations incl. `comparison_basis` + `checked_at` | Sparse **ask-side** observations (lowest comparable listing) |
| `last_alerted_status` | Alert snapshots with `top_price`, `my_bid`, ts | Event-aligned prices |
| `config/listings*.py` @ same commit | `listing_id → market_hash_name/sticker_name/applied_stickers/min_bid/max_bid/tier` | The join key + **policy version** (limits change over time; marketdown 70% since Oct 8) |

`outbid_stats.json` (also checkpointed): `times_outbid`, `last_top_price`, per-branch stats.

**How to mine it:** `git log -p data/state.json` in an **unlocked** worktree — the git-crypt diff filter decrypts each blob for diffing. Caveats: (a) `master` was history-compacted 2026-08-08 — pre-August history lives on branch `archive/pre-compaction-master-20260808` and in `data/backups/csfloat-monitor-history-backup-*.bundle`; (b) observation cadence is per-tier (hot=15 min, mid=90 min, cold=3 h) with a 60-observation/run fair cursor, and commits skip on empty diffs — weight metrics by Δt, not per-run counts; (c) `listing_id`s rotate — join via `tracking_key`/config at the same commit; (d) latest-wins overwrite → history exists *only* in git history.

**What is NOT in the repo:** (a) a durable ask-price series (only the sparse `temporary_max_bids` + alert snapshots — the rich per-item log goes to a private Discord webhook, see §7); (b) fills/acquisitions; (c) resale outcomes.

**Fills & resales live in the auto-lister repo + CSFloat account:**
- `auto-lister` checkout `data/listing_ledger.json`: `{contract_id → {asset_id, name, cost(cents), detected_price, first_seen_at, detected_sold_at, status…}}` — the realized buy→sell ledger; `data/cost_basis.json` (684 active / 3,177 historical items); PnL workbook CSV (~550 rows w/ `csfloat.com/item/{contract_id}` links).
- `GET /api/v1/me/trades?role=buyer&state=verified&limit=1000` — our **1,340 fills** in ~3–27 requests (key required). This is the audit's label set.

---

## 3. External price sources — full comparison

### 3a. Retroactive history (covers June–Nov 2026 today)

| Source | Endpoint | Data | Auth | Cost | ~3,000-item feasibility | Coverage of our window |
|---|---|---|---|---|---|---|
| **CSFloat `/history/{mhn}/graph`** | `GET csfloat.com/api/v1/history/{mhn}/graph?paint_index=` | Daily avg **completed-sale** price + daily count, back to ~2021 | none (key reduces ban risk) | free | 1 req/item; ~1–10 h at route limits | ✅ full window, daily; thin on illiquid items (`count` exposes it) |
| **CSFloat `/history/{mhn}/sales`** | same path `/sales` | Per-sale records w/ float, paint_seed, phase, **full sticker set** | none | free | 1 req/item but **tail only** | ⚠️ recent weeks only; unusable for June–Oct on liquid items |
| **Steam `pricehistory`** | `steamcommunity.com/market/pricehistory?appid=730&market_hash_name=` | Daily median sale + volume, **item lifetime** | `steamLoginSecure` cookie | free | ~20 req/min sustained → ~2.5–5 h, brittle (escalating soft-bans) | ✅ full window; buyer-paid price ÷~1.15 for net; no float/sticker split |
| **market.csgo.com `full-history`** ⭐ | `GET /api/v2/full-history/all.json` (index, no key) then `/full-history/{item_id}.json` | **Per-sale rows `[unix_ts, RUB, USD, EUR]`**, ~2–6k recent sales/item + 7d/30d averages | **none** | free | ~3,001 requests, static files, no auth | ✅ most items reach well past June; hyper-liquid items may truncate early window |
| **BUFF163 internal `bill_order`** | `buff.163.com/api/market/goods/bill_order?goods_id=` | Per-sale history, months deep | CN-account session cookie + Cloudflare | free | ~3k paged calls, fragile, IP bans | ✅ but most operationally painful route |
| **Pricempire Enterprise** | `GET api.pricempire.com/v4/paid/items/prices/history` | Timestamp→price map per market (40+ providers incl. `csfloat`, `buff163`, `steam`), **max 180-day window**, mhn-keyed CSV filter | key | $239.90/mo | **~2 calls/market for the entire catalog** (window cap) | ✅ if archived — easiest bulk multi-market history that exists |
| **CS2Cap Pro** | `api.cs2c.app/v1/prices/candles` | OHLCV candles ≤365d (5m/1h/1d) + `/sales`, 41 markets | key | $79/mo | batchable | ✅ since ~Apr 2026 |
| **cs2.sh Scale** | `api.cs2.sh/v1/…archive` | OHLC + **archived CSFloat full order books + sales since 2022**; mhn-keyed schema | key | $200/mo | unlimited @10 rps | ✅ strongest independent CSFloat-book reconstruction |
| **CSPriceAPI Enterprise** | `api.cspriceapi.com/v1/…` | Daily history 1yr+ (13 markets) + **11M completed-sales archive (CSFloat/YouPin/Skinport) w/ float/stickers** | key | $225/mo | 10 rps | ✅ the only *per-sale CSFloat* archive for sale |
| **csgoskins.gg Business** | `/api/v1/price-histories` | Daily listing-price series + volume, 40 markets | key | €279/mo | per-item/per-market | ✅ 365d (Pro tier's 90d is insufficient) |

### 3b. Bulk current benchmarks (free; useful for forward collection, not retroactive)

| Source | What one request gets you |
|---|---|
| **CSFloat `/listings/price-list`** | `{market_hash_name, quantity, min_price}` for **all items** — 1 call |
| **Skinport `/v1/items`** | whole catalog: min/max/mean/median/suggested price + `version` (Doppler phase) — 1 call, no auth (`Accept-Encoding: br` required) |
| **Skinport `/v1/sales/history`** | all items' rolling min/max/avg/median/volume for **24h/7d/30d/90d** — 1 call; cannot rewind (start a daily snapshot collector) |
| **market.csgo.com `prices/USD.json` + `prices/orders/USD.json`** | all-item asks **and all-item top buy orders** — 2 calls, no key. The only free bulk **bid-side** market feed |
| **Waxpeer `/v1/prices?highest_offer=1`** | all items' min price + best offer — 1 call |
| **WhiteMarket `export.white.market/v1/prices/730.json`** | all items incl. float/seed/inspect, keyed by (mhn, paint_index) — phases split correctly |
| **csgotrader.app daily dumps** | `prices.csgotrader.app/latest/prices_v6.json` all items × several markets; **dated archive `…/YYYY/MM/DD/prices.json` claims history back to 2019** — verify 2026 dates still resolve before relying on it |
| **BUFF official `/api/market/items`** | full >20k catalog min prices; `/float/items` adds **float-band price floors** (rare + very relevant to our bids) — Bearer dev token, 500 req/day |

### 3c. Dead or useless for this audit

- **BitSkins: permanently closed** (announced 27 May 2026, off 30 June/14 July) — mid-window death; no history source.
- **ByMykel CSGO-API**: contains **zero prices** (author declined, issue #34) — useful only as a `market_hash_name`/paint-index/wear metadata map (`skins_not_grouped.json`, `all.json`).
- Swap.gg, TradeIt, cs.money proper: no public APIs — aggregator-only.
- Kaggle/Zenodo datasets: the best (Zenodo CS2 daily panel, 865 Steam items 2013→Aug 2026) **ends mid-window** — cross-check only.

---

## 4. Rate-limit reality check (CSFloat)

No published table; limits are **per-route**, reported via `X-RateLimit-Limit/Remaining/Reset` per response. Only observed number: `GET /listings` ≈ 5 req/min unauthenticated; read routes are "generous" vs writes. Two stacked limiters: IP-based (per 5 min) + per-route. Drive pacing from the headers, never fixed delays; `429` → wait for `X-RateLimit-Reset`. Feasibility for the `/graph` pull: worst case ~10 h, plausibly ~1–2 h with a key — run overnight. `Authorization: <api_key>` (raw key, no `Bearer`).

---

## 5. SPEC — recommended pipeline

### Phase 0 — Internal reconstruction (no external calls, biggest ROI)

1. Extract per-commit series: for each `master` (+ `archive/pre-compaction-*`) commit, parse `data/state.json` → `fact_run_obs(listing_id, commit_ts, top_comp_bid, my_bid, scheduler fields)`; parse `config/listings*.py` @ same commit → `dim_item(listing_id→tracking_key, market_hash_name, sticker_name, applied_stickers, min_bid, max_bid, tier, quantity, bid_war)` and `dim_item_policy_version` (captures marketdown-era 70% ceilings).
2. Fills: `GET /me/trades` (role=buyer, verified) + auto-lister `listing_ledger.json`/`cost_basis.json` + PnL CSV `Link` (contract_id) → `fact_fill` + `fact_resale`.
3. Compute positioning + opportunity cost entirely in-repo (metrics §6). This alone answers "did we hold top-of-book / lose winnable fills" for every observed item.

### Phase 1 — External ground truth (free, ~1 day of pulling)

1. **CSFloat `/history/{mhn}/graph`** for the union of: all `market_hash_name`s ever in config/listings (reconstruct universe per commit — survivorship!), all filled items, all archive/SOS items. → `fact_market_ref(venue=csfloat, day, avg_sale, count)`.
2. **Steam `pricehistory`** for the same mhn set (or liquid subset if cookie ops get annoying). → `fact_market_ref(venue=steam, day, median_sale_net, volume)` with ÷1.15 fee normalization.
3. **market.csgo.com `full-history`** per-sale pulls **on demand** for items needing sale-level evidence (fills we flagged as possible overpay, items where CSFloat `count` is thin) — plus `all.json` index for the mhn→item_id map.
4. Optional: **BUFF `bill_order`** only if CSFloat+Steam leave material gaps on liquid items; skip if catalog is souvenirs (it is).

### Phase 2 — Stickered-signature valuation (the hard core)

The catalog is mostly souvenir weapons with baked-in Kato14/Cologne/All-Stars stickers. mhn-level prices are only a *base*. Per item, benchmark = **Tier A**: median of last N≥3 same-signature CSFloat sales in ≤90–180d window (match on mhn + sticker-ID multiset + wear; slot for position-priced stickers; souvenir edition must match). Tier A is only fully available for the recent `/sales` tail → for June–Oct use **Tier C/D**: CSFloat `reference.predicted_price` (from `/listings` item objects — same appraiser family the market shows buyers) or base + Σ(sticker_price·SP%) with sticker prices from the sticker mhn's own series (SP% classes: Kato14 holo ~5–15%, Kato14 paper ~3–8%, modern ~0.5–2%; treat souvenir-baked sets as item identity, not additive). **Blue gems / rare seeds / best-position Kato crafts: mark non-comparable — score them on realized outcomes only.** If Phase 1 evidence shows too many "ungradeable" items, escalate to paid **cs2.sh Scale** (archived CSFloat books+sales) or **CSPriceAPI** (11M sale records w/ stickers) rather than guessing.

### Phase 3 — Forward collection (cheap cron, fixes the future)

Daily snapshot (GitHub Action or supervisor cron): Skinport `/v1/sales/history` (1 req, all items), CSFloat `/listings/price-list` (1 req), market.csgo.com `prices/USD.json` + `prices/orders/USD.json` (2 req). Commit as append-only parquet/CSV — every future audit gets per-market history for free. Cost: ~4 requests/day.

### Data schema (as researched)

```text
dim_item(item_key, tracking_key, market_hash_name, wear_tier, is_souvenir,
         sticker_signature_id, float_min, float_max, paint_seeds, paint_index,
         first_seen_commit, last_seen_commit)
dim_item_policy_version(item_key, commit_sha, valid_from_ts, tier,
         min_bid, max_bid, temp_max_bid, quantity, auto_bid, bid_war,
         shared_bid_group, marketdown_factor)
fact_run_obs(item_key, run_ts, tier, my_bid, top_comp, lowest_ask?,
         effective_max, was_outbid, bid_updated, rate_limited, commit_sha)
        -- grain: item_key × run_ts (commit time); ask-side sparse (temp_max_bids only)
fact_fill(fill_id, item_key, asset_id, contract_id, fill_ts, qty, fill_price,
         our_bid_at_last_obs, top_at_last_obs, settle_ts, settle_status)
fact_resale(asset_id, item_key, resale_ts, venue, gross, fee_rate, net, hold_days)
fact_comp_sale(venue, sale_ts, market_hash_name, asset_id?, float_value,
         paint_seed, paint_index, sticker_ids, sticker_slots, sticker_wear,
         price_usd, source)
fact_market_ref(mhn, day, venue, avg_sale?, median_sale?, count)
dim_fx(day, cny_usd)   -- only if BUFF/YouPin data enters
```

Joins: fill → last `fact_run_obs` ≤ fill_ts (tolerance ~2× tier cadence); comp-sale → item via comparability rules; market_ref asof on mhn.

---

## 6. SPEC — metrics (all computable from the schema)

| Question | Metric | Definition |
|---|---|---|
| A. Positioning | `TopOfBook share_i` | time-weighted Σ 1[my_bid > top_comp]·Δt / ΣΔt per item (weight by Δt — uneven tier cadence) |
| | `Tick-gap dist` | distribution of `top − my_bid` on losing runs; mass at ≤1 tick = penny-war losses |
| | `Margin of victory` | `(my_bid − top)/top` when winning; high median ⇒ bidding more than needed |
| B. Fill quality | `IS_f` (implementation shortfall) | `fill_price − B(item, fill_day)` per fill; **resolve fill-price semantics first (P0 below)** |
| | `Discount-to-ask` | `(lowest_ask@t⁻ − fill)/ask` — spread captured vs concurrent floor |
| C. Adverse selection | `Markout_f(h)` | `B(item, t_f + h) − fill_price` for h ∈ {7d (Steam trade-hold boundary — earlier marks are informational only), 30d, at resale}; subtract market-index move to isolate alpha from beta |
| | Conditional fill test | `P(fill | ask dropped X% recently)` vs baseline — elevated = sellers dumping into bids |
| D. Opportunity cost | `MissedFillCost_i` | Σ over winnable losses (`top ≤ effective_max`, needed `top+1tick` affordable) of `B − needed`; compare vs `Σ(B − fill)` realized |
| E. Verdict rubric | per item | Well-bid / Overpaying (persistent `IS<0`, fills at `max_bid` in bid-war state) / Losing-winnable (≤1τ losses, `needed ≤ max`) / Right-but-unlucky (markout ≈ index, beta-driven) / **Ungradeable** (<3 comps in 180d → report realized only) |

**Normalization rules:** USD cents everywhere; fee-adjust before cross-venue comparison (CSFloat 2% seller fee — our resale venue; Steam pricehistory ÷~1.15; Skinport fee changed 12%→8% mid-2025 — outside window but check). Compare our CSFloat fills to CSFloat-computable benchmarks primarily; other venues for direction/gap-filling only (net-of-vig adjustments are approximate). Attribute each fill to the last run observation before `sold_at`.

**Pitfall mitigations baked into the spec:** survivorship (universe reconstructed per-commit), stale reads (`last_prices` = last-seen, distinguish via diffs/rate-limit flags), circularity (value benchmarks from *sales*, positioning from the *book* — never judge bid level by the book it sits in), matched≠settled (join fills to completed trades only), marketdown contamination (policy version carries the 0.70 factor — do not score post-Oct-8 ceilings as mispriced bids), branch items (audit at item level, `top` may legitimately exceed max on unwinnable branches).

---

## 7. What could NOT be spec'd — open items for the operator / follow-up agent

| # | Unknown | How to resolve |
|---|---|---|
| U1 | **Fill-price semantics**: does a CSFloat buy-order fill charge our bid or the listing price? The overpay metric depends on it. | Empirical: compare first ~50 fills (`/me/trades` contract price) vs `my_bid`@last-obs and observed lowest listing. |
| U2 | Real `X-RateLimit-Limit` on `/history/*` and `/listings/price-list` with our account key — determines whether the graph pull is 1 h or 10 h. | One live probe request, read the headers. |
| U3 | Per-sale depth achievable for stickered items — `/sales` tail length varies by item liquidity; how many June–Oct sales are actually reachable for our universe? | Probe `/sales` on 10 representative items (1 liquid commodity, 3 mid souvenirs, 6 illiquid Kato souvenirs). |
| U4 | **ToS/account-risk decision** on bulk CSFloat pulls + whether to buy a month of Pricempire-Enterprise/cs2.sh/CSPriceAPI (~$200–280) if gaps demand per-sale or multi-market history. | Operator decision — budget + account risk appetite. |
| U5 | **Discord private log** — the richest per-item series (observed top bid + lowest-comparable-ask per run) goes to a private webhook. Is it exportable/backfillable? If yes it fills the ask-side gap without external calls. | Check webhook/channel export options; Actions logs themselves only carry RUN_EVIDENCE aggregates and expire. |
| U6 | `csgotrader.app` dated dump availability for Jun–Nov 2026 + SteamDT signup feasibility outside China. | `curl -I https://prices.csgotrader.app/2026/09/01/prices.json`; try SteamDT account creation. |
| U7 | Actual per-item CSFloat `graph` data density — how many souvenir items have `count=0` for most days (i.e., how much of the catalog is "Ungradeable" by sales)? | Comes free with the Phase-1 pull; estimate ~30–50% of the souvenir catalog. |
| U8 | Fill→resale join completeness — the PnL CSV is hand-maintained; `listing_ledger.json` is machine-generated but only covers items listed through auto-lister. Some buys may have sold elsewhere or remain unsold. | Reconcile `/me/trades` (buyer) ↔ ledger ↔ `/me/trades` (seller); flag orphans as `resale=unsold` and mark to current `B`. |
| U9 | Whether Secondary-bot (beaconsong twin repo) data should be included — same methodology applies, but its state history lives in the second repo/account. | Operator decision; if yes, repeat Phase 0 there (its `state.json` is equally checkpointed). |
| U10 | Whether `data/active_bid_prices.csv` (≈3,940 rows) vs `.json` (109 items) mismatch is real staleness or a format difference — affects how much of the catalog the export feeds. | `git log` the two files; check `export_bid_prices.py` write conditions. |

---

## 8. Execution prompt (hand this to the implementing agent)

> Build `tools/price_effectiveness_audit.py` on branch `audit/effectiveness-review`. Phase 0: walk `git log` (master + `archive/pre-compaction-master-20260808`) in an unlocked worktree; for each commit parse `data/state.json` (`last_prices`, `my_bids`, `bid_scheduler`, `temporary_max_bids`, `last_alerted_status`) and `config/listings*.py` → emit `fact_run_obs` + `dim_item` + `dim_item_policy_version` (schema in §5, .md doc §5). Join fills from `GET /api/v1/me/trades` + auto-lister `data/listing_ledger.json`/`cost_basis.json` + PnL CSV `Link` column. Phase 1: pull `GET /api/v1/history/{mhn}/graph` for the reconstructed mhn universe (pace on `X-RateLimit-*`, cache raw JSON per item under `data/reference/market/`); pull Steam `pricehistory` for liquid items (steamLoginSecure cookie, ≤20 req/min); pull market.csgo.com `full-history` per-sale JSONs on demand for flagged items. Phase 2: classify comps per §4–5 comparability rules; score metrics per §6; classify items per verdict rubric. Emit `docs/effectiveness_price_audit.md`. Open items U1–U10 above must be probed/answered inside the run; anything still unanswerable gets listed as `confidence=low` in the report, never guessed.

### Sources

- CSFloat API: `docs.csfloat.com`, unofficial `csfloatapi.com` (sales/graph/listings/buy-orders/me-trades specs), `csfloat-node-sdk` API coverage notes (price-list, graph depth probes), `csfloat-rs` docs.rs.
- Skinport: `docs.skinport.com` (`/v1/items`, `/v1/sales/history`, websocket sale feed, rate limits).
- Steam Market: InternalSteamWebAPI wiki, steampy, community-measured ~20 req/min `pricehistory` limits.
- market.csgo.com: `market.csgo.com/docs-v2` (`full-history`, `prices/orders` dumps).
- BUFF: `buff163api.com` mirror of official developer API + internal `bill_order`/`price_history` routes; goods_id maps via `EricZhu-42/SteamTradingSite-ID-Mapper`, `ModestSerhat/cs2-marketplace-ids`.
- Aggregators: `pricempire.com/docs`, `cs2cap.com`, `cs2.sh/llms-full.txt`, `cspriceapi.com`, `csgoskins.gg/api`, `steamdt.com` (doc.steamdt.com, zh-only), `openskin.dev`, `skinpricer.com`.
- Methodology: Perold implementation shortfall; markout/adverse-selection literature (Databento/Nasdaq markout docs); SteamAnalyst/skin.place sticker-value guides; Steam souvenir-guide multi-factor pricing; AdvancedFloat liquidity tiers.
- Internal: `src/market_monitor/monitor_market.py` (`get_competing_bids`), `monitor_runtime.py::_build_state_payload`, `.github/scripts/checkpoint_runtime.sh` (11-file allowlist), `tools/effectiveness_audit.py`, auto-lister `data/listing_ledger.json`/`cost_basis.json`.
