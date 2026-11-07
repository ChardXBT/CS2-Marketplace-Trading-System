# Implicit State Machines — Audit

Repo: CS2-Marketplace-Trading-System (audit branch `audit/implicit-state-machines`, base `origin/master`).

**Encryption note:** this repo is git-crypt encrypted (`*.py`, `*.json`, `*.env`,
`*.txt`, `data/**` per `.gitattributes`). The audit worktree was unlocked with
the key present in the main checkout (`git-crypt` needed its key copied into
the worktree git dir, since linked worktrees do not share `.git/git-crypt/`).
All Python sources below were audited in plaintext.

Scope: `src/market_monitor/*.py` (~17k LOC) plus `config/` loaders, `main.py`,
`tools/`. `data/reference/` skeletons and generated data files excluded.

The system is a CSFloat buy-order monitor. It has no declared state model for
remote orders: order/listing lifecycle is reconstructed every run from API
field presence/absence, per-item config booleans, raw status strings persisted
in `data/state.json`, and *which config file* an item dict lives in.

---

## 1. Missing buy-order disposition (API-field inference)

**States.** `live` / `missing→recreate` / `missing→one-time-terminal` /
`missing→sos-parked` / `missing→temp-dead-tracked`.

**Encoded as.** `get_my_bid_from_api(item)` returns `float | None`; `None`
conflates at least five distinct remote conditions (order vanished/filled,
never existed, exists but fails `_order_matches_item`, exists but `price`
field absent → `order_id_to_price` fallback, one-time `_finalize_one_time_bid`
failure). `None` alone then drives a three-way disposition chosen by a
~10-condition flag matrix:
`_can_attempt_recreate_buy_order` = `market_hash_name` present AND
`recreate_bid is True` AND `_one_time_bid_spent is not True` AND no
`sticker_ids`/`sticker_alternatives`/`sticker_groups` AND `rarity_exclude is
None` AND `rarity is None` (monitor_market.py:3208–3225); `_one_time_bid_spent`
= `one_time_bid is True` AND (`_one_time_bid_spent` OR `recreate_bid is False`
OR `lid in state["one_time_bid_creations"]`) (monitor_market.py:3092–3100).

**Invalid combos.** `one_time_bid=True, recreate_bid=True` with a lost/corrupt
`one_time_bid_creations` record → recreates a bid that was meant to be
one-shot (double purchase). `temporary_suspend=True` + missing order → SOS
park while its rotation entry still says `TEMP_ACTIVE`. `buy_order_id` set
but pointing at a *different* item's order: treated as "no order" → recreate
while the real order still lives (duplicate live orders).
`result["missing_buy_order"]=True` combined with `skip` unset is representable
and would crash downstream (`r["my_bid"]` KeyError, monitor_processing.py:1962).

**Transitions.** monitor_processing.py:816–869 (branch on `my_bid is None`:
824 recreate-success, 827 temp-dead record, 835 one-time terminal, 849
`_move_listing_to_sos`); monitor_market.py:488–513 (startup SOS check);
monitor_market.py:1236–1298 (None-producing paths); temp-dead tracker
monitor_processing.py:615–641 (`data/temp_dead_listings.json` presence).

**Risk.** HIGH. A misplaced item flag or a lost state record turns "missing"
into the wrong disposition: recreate a one-time bid (unwanted purchase), or
park to SOS and strand a listing the operator expected live.

---

## 2. One-time bid lifecycle (flags across 3 stores)

**States.** `pending` (never bid) / `active` (order confirmed live) /
`spent-terminal` (order taken, never recreate) / `persisted` (durably
disabled in config).

**Encoded as.** Four flags in three different stores: `one_time_bid` (config),
`recreate_bid` (config file, rewritten by the bot), `_one_time_bid_spent` and
`_one_time_bid_policy_persisted` (runtime-only, synthesized at startup), plus
`state["one_time_bid_creations"][lid]` record presence (state.json).
`_one_time_bid_spent` is *derived* each boot by OR-ing three of them
(monitor_market.py:3092–3100).

**Invalid combos.** 16 boolean combinations; only ~4 are intended.
`_one_time_bid_spent=True, recreate_bid=True` → `_can_attempt_recreate_buy_order`
still blocks, but any code path checking only `recreate_bid` recreates.
`_one_time_bid_policy_persisted=True` while config still says
`recreate_bid=True` → on restart `_restore_one_time_bid_policies`
(monitor_market.py:3112–3113) re-derives spent from the config flag alone,
so a config edit silently resurrects or kills the lifecycle.

**Transitions.** monitor_market.py:3143–3205 (`_finalize_one_time_bid`: sets
both runtime flags, writes state record, rewrites `recreate_bid: False` in the
config `.py` file via AST text edit); 3102–3113 (startup derivation);
monitor_processing.py:835–843 (terminal read); monitor_market.py:1246–1250,
1277–1281 (finalize on bid lookup).

**Risk.** HIGH — this is the money-path guard. The three-store split exists
precisely because each store can fail independently; the OR-of-flags read
means the machine *self-heals*, but any fourth write site that forgets one
store (or a crash between `item[...] =` at 3175–3176 and the two persistence
writes at 3182/3186) leaves inconsistent state that is representable and only
partially detected by `_stop_after_confirmed_write_bookkeeping_failure`.

---

## 3. Temporary-bid rotation lifecycle

**States.** `rotation["status"]` ∈ {`TEMP_ACTIVE`, `MAIN_SUSPENDED`,
`TEMP_GONE_RESTORE_PENDING`, `RESTORING_MAIN`, `MAIN_RESTORED`,
`RESTORE_BLOCKED`} — module-level string constants
(temporary_bids.py:72–77), **but compared against raw literals in another
file**: monitor.py:528–533 spells `"MAIN_SUSPENDED"`, `"TEMP_GONE_RESTORE_PENDING"`,
`"RESTORING_MAIN"`, `"RESTORE_BLOCKED"` inline.

**Encoded as.** `state["temporary_bid_rotations"][temp_key]["status"]` plus
field-presence companions: `temporary_order_id`, `main_order_id`,
`restored_order_id` (present/absent = which remote orders exist),
`main_pre_removed_at`/`temporary_pre_removed_at` (set only for some entry
paths, 734–736), `last_error` (set/blocked marker). Persisted validation only
requires `status` to be a non-blank string (state.py:456–461) — a typo or
stale value survives restarts and silently matches nothing.

**Invalid combos.** `status=TEMP_ACTIVE` while `temporary_order_id` is absent
from the live book (bot detects this per-run but the persisted combo is
representable); `status=MAIN_SUSPENDED` + `restored_order_id` set;
`RESTORE_BLOCKED` + `temporary_suspend` removed from the main item (validator
monitor.py:523–537 then demands a `buy_order_id` that was intentionally
deleted). A renamed constant value would silently break only the
literal-comparison site in monitor.py.

**Transitions.** temporary_bids.py:713–732 (initial status picked by
`*_was_already_absent` flags), 760/859 (→`RESTORE_BLOCKED`), 832/938
(→`MAIN_SUSPENDED`), 852/1017/1026 (→`TEMP_GONE_RESTORE_PENDING`), 996/1039
(→`RESTORING_MAIN`), 1043 (→`MAIN_RESTORED`); reads at 380, 451–460, 527, 584,
741, 806, 824, 918; monitor.py:523–537 (config validator reads the same
strings to excuse a missing `buy_order_id`).

**Risk.** HIGH. A rotation stuck mid-lifecycle leaves a *real money* pair in a
bad place: main suspended forever (lost bids) or temp+main both live
(over-exposure). `RESTORE_BLOCKED`/`last_error` depend on every run
re-evaluating; a status string that no site recognizes keeps the pair frozen
with no alarm beyond a one-shot Discord message.

---

## 4. Managed-overlay slot lifecycle

**States.** `slot["status"]` ∈ {`OVERLAY_ACTIVE`, `DONOR_ACTIVE`,
`DONOR_SUSPENDED`, `OVERLAY_RECREATE_PENDING`, `OVERLAY_BLOCKED`,
`OVERLAY_RETIRE_PENDING`, `RETIRING_OVERLAY`, `DONOR_RESTORE_PENDING`,
`RESTORING_DONOR`, `DONOR_RESTORED`, `RETIREMENT_BLOCKED`} — 11 module-level
string constants (managed_overlays.py:24–34).

**Encoded as.** `state["managed_overlay_slots"][key]["status"]` + pairing
fields `overlay_key`/`donor_key`/`overlay_order_id`/`donor_order_id`/
`restored_order_id` presence, plus config-side item flags `managed_overlay`,
`managed_overlay_suspend`, `slot_owner_tracking_key` that re-pair the slot to
config each run via alias matching (managed_overlays.py:44–119). Same weak
persisted validation as rotations: non-blank string only (state.py:456–473).

**Invalid combos.** `status=DONOR_SUSPENDED` with donor item edited out of
config (slot orphans, `_find_donor_by_key` returns None → restore paths can't
resolve); `OVERLAY_ACTIVE` + `slot_owner_tracking_key` repointed at a
different donor mid-cycle; two overlays mapped to one donor is invalid yet
representable (only caught by `_validate_managed_overlay_config`, 155–165).
Retirement states coexist with `temporary_suspend` on the same donor unless
validators happen to run.

**Transitions.** managed_overlays.py:384/462 (retire block/pending),
491/507/511/832/903/924/931/944/948 (restore sequence →`RESTORING_DONOR`→
`DONOR_RESTORED`), 532/794 (→`DONOR_RESTORE_PENDING`), 606/616/658/662/673/974
(→`OVERLAY_BLOCKED`), 703/709 (→`OVERLAY_ACTIVE`/`DONOR_ACTIVE`), 729
(→`OVERLAY_RECREATE_PENDING`), 784 (→`RETIRING_OVERLAY`); terminal reads
78, 237, 576, 756, 805.

**Risk.** MED-HIGH. The 11-state machine controls which of two orders is
allowed to exist remotely; a stuck `*_PENDING`/`*_BLOCKED` slot with no
retry path parks both sides, and the config-vs-slot re-pairing by alias means
a config edit can silently re-target a live slot at a different donor.

---

## 5. MutationCandidate action/category string dispatch

**States.** `candidate.action` ∈ {`price`, `quantity`, `recreate`,
`suspend_main`, `restore_main`, `managed_overlay_suspend`,
`managed_overlay_recreate`, `managed_overlay_restore_donor`,
`managed_overlay_retire_delete`, + any future string}; `candidate.category` ∈
{`safety`, `restoration`, `outbid`, `repair`, `quantity`, `decrease`, *other*}.

**Encoded as.** Free-form strings on the `MutationCandidate` dataclass
(bid_scheduler.py:19–52). Category is *inferred* from action+price direction
when not supplied (monitor_processing.py:101–109: `action=="recreate"`→
`"repair"`, `target>current`→`"outbid"`, else `"decrease"`). Action dispatch
is `startswith`: monitor_processing.py:455–468 —
`"managed_overlay_"` prefix → overlay executor; **everything else falls
through to the temporary-bid executor**.

**Invalid combos.** An action string that matches neither known name nor the
prefix is silently routed to `_execute_temporary_bid_candidate` — a typo is a
wrong-order mutation, not an error. `category="safety"` on a `price` action
is representable and jumps the mutation budget queue (433,
`HIGH_PRIORITY_CATEGORIES` bid_scheduler.py:15). Category strings also cross
into persistence (`persisted_hint` → `scheduler_state["pending"]` → re-read
as `hint.get("category")` at bid_scheduler.py:148–159), so a stale persisted
category steers scheduling forever.

**Transitions.** Writes: monitor_processing.py:102–109, 706, 893
(monitor_runtime), temporary_bids.py:812–866, managed_overlays.py:468–734.
Reads: monitor_processing.py:402/415/433/455–468/478–486,
bid_scheduler.py:82–108/148–159, temporary_bids.py:926/950,
managed_overlays.py:750/800/954/993.

**Risk.** MED. New action names are a landmine: no exhaustiveness check
anywhere, wrong executor silently. Category mislabel reprioritizes or
starves real-money mutations.

---

## 6. Competitor-book channel tri-state (type+sentinel)

**States.** `competitor has top bid` (float>0) / `book empty` (`top == 0.0`) /
`rate limited` (`top == "rate_limited"`) / `API error` (`top is None`).

**Encoded as.** `get_highest_competing_bid` returns
`tuple[float | str | None, str | None]` (monitor_market.py:2595–2683); the
literal `"rate_limited"` is a magic sentinel inside a nominally-numeric
channel, and `0.0` doubles as "empty book". Inner helpers
(`_check_buy_order_top_bid`-family) also return `"rate_limited"` strings
(monitor_market.py:2352, 2367) and per-listing snapshots store the same
sentinel as a *dict value* (2472, 2538–2553).

**Invalid combos.** Any consumer that does `float(top)` or `top > x` before
checking the sentinel crashes or misprices; a consumer that checks `top is
None` but not `"rate_limited"` treats rate-limiting as an error-skip
(different recovery). `("rate_limited", listing_id)` is representable but
nonsense; `0.0` vs `None` distinction is easy to drop.

**Transitions/reads.** monitor_processing.py:882–894 (sentinel→last-price
fallback vs error→skip), 1264, 1357; monitor_market.py:2465–2475,
2538–2557, 2642–2657, 2683; `top == 0.0` handled at monitor_processing.py:936
(hold/decay state machine input) and 2023 (ONLY BIDDER).

**Risk.** MED. The sentinel conflates transport failure with market data;
every new call site must replay the same three-way check, and the empty-book
`0.0` feeds the bid-war hold/decay counters — mixing them up triggers
wrong-direction bid moves.

---

## 7. Bid-war phase counters

**States.** `idle` / `war-anchored` / `empty-book anchor-hold` /
`stepping-down` / `post-outbid-hold` per listing.

**Encoded as.** Six persisted per-lid fields in state.json:
`bid_war_anchor_active` (bool), `bid_war_counters` (int),
`bid_war_stepdown_counters` (int), `bid_war_zero_counters` (int),
`post_outbid_hold_counters` (int), `bid_war_prev_bids` (float floor). Phase =
which combination is non-zero/present; no field names a phase.
Committed/rolled back via `strategy_effects` tuples
(monitor_processing.py:189–230).

**Invalid combos.** `anchor_active=True` + `post_outbid_hold>0` +
`zero_counters>0` all coexist — the code relies on check-ordering
(post-outbid hold returns early at 941–955, zero-hold at 957–979, stepdown
later) rather than exclusivity. `anchor_active=True` with `bid_war_prev_bids`
missing → `lid in self.bid_war_prev_bids` guard at 961 is the only thing
between the machine and a NameError-style floor. Counters persist across
config edits (`bid_war` threshold may be removed while counters remain).

**Transitions.** monitor_processing.py:920–926 (shared-group reset),
940–949 (hold decrement/pop), 969–970 (zero++), 1012–1016 (war clear),
1092/1105 (war reset), 1110 (zero pop), 1117–1160 (anchor read, stepdown
++/--), 1213+ (hold decrement), 197–230 (effects tuples for atomic commit).

**Risk.** MED. Escalation phase lives in counters nobody audits as a unit; a
stale `post_outbid_hold` after a config price cut keeps the bid pinned high,
and counter drift between the six fields produces hold-forever or
step-down-too-far pricing.

---

## 8. Per-item kind flags (item taxonomy by boolean combo)

**States.** `regular` / `temp-extra` / `temp-suspended main` /
`managed overlay` / `suspended overlay donor` (+`managed_overlay_persistent`
modifier, `one_time_bid` modifier).

**Encoded as.** Independent item booleans — `temporary_extra`,
`temporary_suspend`, `managed_overlay`, `managed_overlay_suspend`,
`managed_overlay_persistent`, `one_time_bid`, `auto_bid`, `recreate_bid` —
plus `slot_owner_tracking_key` string presence for donor pairing
(managed_overlays.py:107–119). Kind = conjunction, e.g. donor =
`managed_overlay_suspend is True AND managed_overlay is not True`
(managed_overlays.py:56–61).

**Invalid combos.** `temporary_extra=True` + `temporary_suspend=True`
(temp can suspend its own main — rejected only at validation,
temporary_bids.py:256–259); overlay + suspended donor (managed_overlays.py:138–139);
donor + `temporary_suspend` (150–151); `slot_owner_tracking_key` present on a
non-overlay. All are config-representable; validators run once at startup so
a runtime tag mutation (`rotation.pop("temporary_suspend")`,
temporary_bids.py:898–909) can create a combo validators would have rejected.

**Transitions.** Config flags written by the bot: temporary_bids.py:898–909
(remove `temporary_suspend`), 973 (re-add on restore), monitor_market.py:3175,
3188 (`recreate_bid`); read sites monitor.py:449–463, 523–537,
monitor_market.py:480–481, 553, 2086–2087, 2724–2745, managed_overlays.py:56–61,
130–153.

**Risk.** MED. The taxonomy guards which orders may be deleted/suspended;
combo drift (esp. flags the bot itself toggles mid-lifecycle) can mark a live
main as a donor or an overlay as suspendable → wrong order deleted.

---

## 9. Safety-scan run state

**States.** `safety_scan["last_scan_status"]` ∈ {`running`, `partial`,
`completed`, `stopped`} — raw string literals, no constants or enum.

**Encoded as.** Status string + `pending_lids`/`scanned_lids` lists +
`next_scan_after` timestamp + `last_scan_items_checked` int + runtime
`self._safety_scan_active` flag + per-item `item["_safety_refresh_success"]`
marker. Coverage is inferred by *which fields exist*
(`_scanned_safety_lids` at monitor_market.py:2120–2142: `scanned_lids` list
present → trust IDs; absent → fall back to `last_scan_status=="completed"`
AND `last_scan_items_checked == len(tracked)`; else rescan everything).

**Invalid combos.** `status="completed"` + `scanned_lids` covering a stale
config (accepted as covered — by design, fragile); `status="running"`
persisted at crash → next boot treats scan as *not completed* (safe here) but
the `"running"`/`"stopped"` distinction is only cosmetic. `next_scan_after`
in the future + `pending_lids` non-empty is representable and ambiguous.
Persisted validation only checks `type is str` (state.py:620–621).

**Transitions.** Writes: monitor_market.py:2175/2219 (`"partial"`),
2229/2315 (`"completed"`), 2257/2266 (`"running"`), 2310 (`"stopped"` or
`"partial"`); `_safety_refresh_success` set 1920/1927/1978, read 2195/2285;
`_safety_scan_active` flips API behavior (retries=1, extra gap) at 1761–1776.
Reads: 2109, 2131; preflight return strings `"none"`/`"inline"`/`"scan_only"`
compared at monitor_processing.py:1801–1812.

**Risk.** MED. The gate decides whether real-money bids run on unvetted
listings; field-presence inference means a partially-migrated state file can
mark coverage "complete" for listings never scanned (count-equality check is
the only guard).

---

## 10. Per-item result outcome flags (single-pass protocol)

**States.** Per-item run outcome ∈ {`skipped-stop`, `skipped-missing-order`,
`skipped-one-time-spent`, `skipped-rate-limited/API-error`, `observed`,
`observed-stale`, `observed-invalid`, `mutated`} plus signal axes
`outbid`/`winning`/`only-bidder`.

**Encoded as.** Optional keys on the `_process_item` result dict:
`skip`, `missing_buy_order`, `one_time_bid_spent`, `rate_limited`,
`observation_valid`, `observation_stale`, `is_outbid`, `was_outbid`,
`tier_signal_outbid`, `tier_signal_decrease`, `bid_updated`, `price_changed`,
`auto_bid_attempted`, `has_winnable_coverage`, `quantity_updated`,
`planned_candidate`, `_strategy_snapshot` — presence *and* truthiness both
carry meaning (`result.get("x", False)` everywhere).

**Invalid combos.** `skip=True` with any signal key set (producer sets both
in some paths — e.g. `rate_limited`+early return elsewhere); `skip` unset but
`my_bid`/`bid_source` unset → hard `KeyError` at monitor_processing.py:1962–1964;
`observation_valid=False` + `observation_stale` unset → reported as
"partial/rate-limited" regardless of real cause (1982–1992); `is_outbid=True`
with `top=0.0` hits the `ONLY BIDDER` branch first (2023) and silently drops
the outbid bookkeeping below it.

**Transitions.** Producers: monitor_processing.py:812–1577 (dozens of
conditional `result[...] =` writes), 394–416 (`_apply_candidate_result`),
446–447 (stale marking). Consumers: monitor_processing.py:1957–2072,
519–556 (`_record_decrease_pressure` reads `tier_signal_*`),
monitor_market.py:2062–2063 (`active_competitor_*` passthrough).

**Risk.** MED. It's an ad-hoc protocol where missing keys mean "false" — the
consumer cannot distinguish "flag never computed" from "computed false";
stale-snapshot vs rate-limited vs API-error all collapse into
`observation_valid=False` with a guessed reason string.

---

## 11. Scheduler pending queue + decrease-pressure counters

**States.** Per listing: `not-queued` / `deferred` (in `pending`) /
`deferred-stale` (`deferred_runs` capped) / `pressure-building`
(`decrease_pressure[lid].confirmations>0`) / `pressure-confirmed`.

**Encoded as.** `state["bid_scheduler"]["pending"][lid]` membership (presence
= deferred) + `hint["deferred_runs"]` int + `hint["category"]` string +
`state["bid_scheduler"]["decrease_pressure"][lid]` `{confirmations,
first_seen_at, last_seen_at}` + `observation_cursor` rotation ints.
`MAX_QUEUE_DEFERRAL_RUNS` caps the counter (monitor_processing.py:130–133).

**Invalid combos.** `pending[lid]` persists across runs while its `category`
/`target_price` hint rots — `pending_watch_items` re-reads the stale hint and
re-prioritizes a listing whose config was edited (bid_scheduler.py:140–160).
`decrease_pressure` with `last_seen_at` older than the gap window is reset
silently (540–542). `deferred_runs` reaches cap but the entry is *kept*, not
dropped — cap semantics differ per reader.

**Transitions.** Writes: monitor_processing.py:438/444/485 (pending set on
defer/fail/stale), 493–513 (merge old pending, `deferred_runs`++),
525–556 (pressure confirmations++/pop), bid_scheduler.py:268
(`observation_cursor`). Reads: monitor_processing.py:118–136,
bid_scheduler.py:117–182, 298–336, monitor_processing.py:1923.

**Risk.** LOW-MED. Deferred mutations are re-driven by persisted hints whose
freshness is only partially re-validated; a stale `pending` entry can push an
obsolete outbid/decrease to the front of the queue.

---

## 12. Tier-by-file-membership + SOS parking

**States.** `hot` / `mid` / `cold` / `sos` / `archive` (+`manual`,
`temporary`, `managed_overlay` special files) — a listing's lifecycle phase
is *which config file contains its dict*.

**Encoded as.** `ITEM_SOURCE_FILES[lid]` map built at import
(listing_loader.py:16–31, 256); tier name re-derived from filename substring
(`"hot" if "_hot" in source else "mid" if "_mid" in source else "cold"`,
monitor_processing.py:125–127). SOS-parked items get stamped
`sos_auto_parked: True` + `sos_reason` fields (monitor_tiers.py:976–977) —
presence = "bot parked this" vs operator-parked.

**Invalid combos.** Same item dict in two tier files (duplicate lid caught at
monitor.py:471–477, but `market_hash_name`-keyed items bypass the `listing_id`
check); item in `listings_sos.py` while a rotation/slot still references it;
`ITEM_SOURCE_FILES` mutated at runtime (managed_overlays.py:254 pops overlay
keys) while `ALL_TRACKED_ITEMS` still holds the item.

**Transitions.** `_move_listing_tier` monitor_tiers.py:847+ (AST text-edit
moves the dict between `.py` files), `_move_listing_to_sos`
monitor_tiers.py:933+ (stamps `sos_auto_parked`/`sos_reason`),
tier moves triggered by `_record_tier_signal` → `pending_tier_moves`
monitor_processing.py:2014–2021/2093–2095; reads via `ITEM_SOURCE_FILES`
at monitor_processing.py:125–127/627/2144, monitor_market.py:3126–3127/3494.

**Risk.** LOW-MED. File membership is authoritative state rewritten by AST
surgery; any divergence between `ITEM_SOURCE_FILES`, the on-disk files, and
`ALL_TRACKED_ITEMS` makes the same listing simultaneously "active" and
"parked" — double-bidding or silent skipping.

---

## 13. Write-outcome & alert status strings

**States.** `method_used` ∈ {`PATCH`, `BID`, `BID_RATE_LIMITED`,
`PATCH_RATE_LIMITED`, `PATCH_RATE_LIMITED_SOFT`, `PATCH_AMBIGUOUS`,
`ORDER_CAP_REACHED`, …}; `edit_err` ∈ {`None`, `rate_limited`,
`rate_limited_soft`, `ambiguous_write`}; `alert_type`/`last_alerted_status[lid].type`
∈ {`outbid`, `winning`, `bid_updated_winning`, `bid_updated_losing`}.

**Encoded as.** `update_buy_order_price` returns `(success, order_id,
method_used)` where `method_used` mixes *mechanism* and *failure class* in
one string; `_edit_buy_order_price` returns a parallel `edit_err` vocabulary
(monitor_runtime.py:444–506). `item['_last_bid_method']` persists the label
on the item dict for later alert text (monitor_runtime.py:914, 1096).
`last_alerted_status` is persisted to state.json for alert dedupe
(monitor.py:175, monitor_runtime.py:709/1130).

**Invalid combos.** New failure labels must be added to both vocabularies and
every `==`/`elif` chain — a missed site falls into the generic
`AUTO-BID FAILED` or wrong retry path. `last_alerted_status` values are only
type-checked as dicts (state.py:583–585) — any `type` string persists.
`method_used="PATCH"` (success label) vs `PATCH_AMBIGUOUS` (failure label)
differ only by suffix; `startswith`/equality slips change alerting.

**Transitions.** monitor_runtime.py:615–622 (edit_err→policy), 905–944
(method_used→retry/cooldown decisions), 1053–1139 (alert type→dedupe write);
monitor_processing.py:740–743 (edit_err passthrough), 2031–2064
(alert_type selection + last_status `!= "outbid"` raw read).

**Risk.** LOW-MED. Retry/stop-the-world decisions ride on string equality
across producer/consumer pairs; an added outcome that isn't dispatched is a
silent wrong-path (e.g., treating a hard rate-limit as transient and
retrying into a longer cooldown).

---

## 14. Marketdown cap-state inference

**States.** Per item: `original` / `reduced` / `pending-cap-reduction` /
`preserved-temp-cap` — inferred, never stored.

**Encoded as.** Global `marketdown` is a bare bool literal parsed out of
`config/market_state.py` via AST (market_state.py:51–65); per-item state is
*re-derived* by float equality against the snapshot baseline:
`item["max_bid"] == reduced_limit(entry["max_bid"])` means "this item is in
the reduced state" (market_state.py:194–218), plus `temp_max_bid` presence as
an orthogonal safety-ceiling marker.

**Invalid combos.** `max_bid` edited to a value that coincidentally equals
`reduced_limit(original)` is indistinguishable from "reduced" — the
cap-reduction exemption activates for an item that was never reduced.
`temp_max_bid > original max` is representable in config and only rejected
when `apply_market_state` runs (173–175). `marketdown=True` + snapshot file
missing → every check silently returns False (except-clause at 201–202)
i.e. global flag on, per-item machinery off.

**Transitions.** `apply_market_state` rewrites `min_bid`/`max_bid` literals in
all ACTIVE_FILES (139–191); exemptions read at monitor.py:540, 547 during
config validation.

**Risk.** LOW-MED. State inferred by price equality means a manual config edit
can flip an item between "reduced" and "exempt" interpretations without
touching the flag — caps silently change in the wrong direction.

---

## Notes / non-findings

- `account_orders.py` itself is clean — it deliberately *refuses* to infer
  state (raises on any pagination anomaly; `AccountOrderSyncCancelled`).
- `bid_source` ∈ {`"API"`, `"API (RECREATED)"`} and `.startswith("API")`
  (monitor_processing.py:824/871/932) is a vestigial 2-value marker — not a
  machine today, but the startswith check implies a dropped third source.
- `cooldown_until`/`patch_disabled_until` epoch timestamps (monitor.py:176/237,
  monitor_runtime.py:611/763) are simple 2-state gates; `patch_disabled_until`
  has no nonzero writer left in `src/` (only tests reset it) — dead-ish
  state field worth removing rather than a machine.
- `data/temp_dead_listings.json` is write-only evidence (nothing reads it for
  decisions) — an artifact, not a machine, but it duplicates the SOS/dead
  signal from finding 1.
- `order_expression` tuple tags (`signature[0]=="expr"`, monitor_market.py:375+)
  are a consistent discriminated AST, not scattered state.
