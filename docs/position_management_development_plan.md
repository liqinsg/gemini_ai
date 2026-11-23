# Position Management — Coding Development Plan

- Date: 2026-11-23
- Status: **PLAN · Owner decisions A/B/C recorded 2026-11-23 (see position_management_review.md §七 Owner 决策记录); Stage 1/2 unblocked, Stage 3+ retain their gates. No deployment authorization.**
- Source of truth: [position_management_review.md](position_management_review.md)
- Scope: `scheduled_runner_v3.py` position-management paths only; no live trading or configuration changes are authorized by this plan.

## 1. Goal and boundaries

Measure and then close the MC-TOPUP cap gap without silently changing the 1→2→4→8 ladder. The existing caps count bot-owned broker trades, not `currentUnits`; enforcement can block later ladder steps without bounding the size of an individual trade. Resolve the weak-exit ownership and OVERRIDE lifecycle decisions before changing those behaviors.

Implementation principles:

- Fail closed when exposure, position count, or post-fill state cannot be established.
- Use the same ownership and `tag_prefix` population as the existing exposure and position-cap logic.
- A broker fill is not assumed to add one trade: reconcile the broker state after each successful top-up before evaluating another pair.
- Strategy caps are not account-wide: manual and other-bot positions are excluded. Do not describe them as total account-risk limits.
- Keep dry-run and test paths incapable of submitting live orders.
- Do not change run.env, live lot sizes, or unrelated entry/exit policies in this work.

## 2. Decisions required before behavior changes

| Decision | Recommendation | If not approved |
|---|---|---|
| A — TOPUP guard and ladder impact | Add `MC_TOPUP_CAP_GUARD=off|shadow|enforce` (default `off`). Shadow the per-trade caps for at least 7 days; record block rate by ladder rung and actual pair units. Enforce only after Owner reviews whether reduced ladder depth is intended. Treat TOPUP as outside the OVERRIDE entry bypass. | Do not enforce; the P0 gap remains open. If a hard account-wide limit across hosts is required, approve shared serialization; snapshots and the optional witness are not atomic. |
| B — weak-exit ownership | B1: exclude non-bot-owned trades before ranking and group-size evaluation; retain close-time gate as defense in depth. | Preserve current ranking population and document that manual positions can affect bot exit ranking. |
| C — OVERRIDE exits | C2: preserve current behavior; OVERRIDE bypasses only the maintain-path early-exit gates, not weak-exit or scale-out. Owner must confirm this risk behavior. | If full exit immunity is intended, select C1 and test it before implementation. |
| D/E — sizing and boost | Measure effective production sizing before assigning D a low risk. E's `BASE>2` warning already exists; preserve it and add a focused log test if touched. | No unrelated behavior changes in this plan. |

## 3. Staged implementation

### Stage 0 — Baseline and approval

1. Record the current branch/commit and run focused MC-TOPUP, exposure, basket, and single-host tests.
2. Obtain Owner decisions A, B, and C; record selected behavior and shadow metrics in the review document before implementation.
3. Read-only inspect the deployed account's fill representation and effective units; do not infer them from code comments.
4. Keep behavior-changing enforcement in a separate commit from behavior-preserving refactoring.

**Exit criteria:** decisions are recorded; baseline tests pass or pre-existing failures are identified and isolated.

### Stage 1 — Compute and pass caps (separate no-behavior-change commit)

Move the MC regime, `_MAX_OPEN_POSITIONS`, and dynamic `_cross_net_cap` calculations to after `_all_trade_pairs`, MC cache loading, and `_global_scores` are ready, but before `_mc_min_position_topup_scan_v31()`. Pass them explicitly as `cross_net_cap`, `max_open_positions`, and `cap_guard_mode`; do not have TOPUP read hidden module globals.

Preserve the calculations and logging; change ordering and dependency passing only. Keep the guard `off` in this commit. Check all dependencies at the new location and ensure basket selection still receives the same values.

**Focused tests:**

- TOPUP sees the current cycle's MC-selected max-position and cross-net caps, not values retained from the previous cycle.
- A cycle with no MC pairs retains the existing no-data/neutral behavior.
- Existing basket cap and override behavior remains unchanged.

**Commit boundary:** land this refactor separately from the helper, shadow instrumentation, or enforcement behavior.

### Stage 2 — Shared helper and shadow instrumentation

Create one typed helper, `_strategy_exposure(trades) -> (net, count, eligible)`, implementing the exact `is_bot_owned_trade` plus `clientExtensions.tag` prefix filter. Use it in:

- `_build_open_exposure()` for `net` and eligible trades, then derive the deduplicated `held` set.
- `_execute_single_signal()` for trade count and same-pair checks.
- MC-TOPUP for eligible aggregation, cap state, and fresh-snapshot reconciliation.

Do not maintain three copies of ownership/tag/count logic. Add a deterministic parity test using seeded/generated trade lists (including manual tags, unmatched bot prefixes, multiple trades per pair, long/short units, malformed records) to prove all existing paths agree with the helper. Preserve each caller's established fail-closed behavior on malformed exposure.

Add validated mode `MC_TOPUP_CAP_GUARD=off|shadow|enforce`:

- `off`: no cap behavior change; safe default until approved.
- `shadow`: evaluate only candidates that pass existing regime, direction, ladder, and SL/TP checks; log structured would-block reason/caps/rung/units, but do not block.
- `enforce`: block eligible candidates that exceed passed caps. Never switch automatically after a timer.

Place the check immediately before the dry-run/order decision, after all qualification gates, to avoid logging cap blocks for candidates that would never submit.

**Focused tests (extend `tests/test_mc_min_position.py` or add a dedicated cap-guard test module):**

- `off` preserves current order decisions; `shadow` logs would-block without blocking; `enforce` blocks at the same cap boundary.
- Cap logs are produced only for candidates that pass regime, direction, ladder, and SL/TP checks.
- Existing eligible trades at the max-position cap block in enforce mode; one below the cap can proceed.
- A single top-up that would cross either currency's net cap is blocked in enforce mode; equality with the cap is allowed, consistent with basket logic.
- Two different pairs cannot cumulatively exceed the same currency cap in one sequential scan.
- Multiple trades on one instrument count individually, even though pair aggregation yields one TOPUP candidate.
- Bot-owned trades without a configured strategy prefix are excluded from both the runtime count and TOPUP aggregation.
- A top-level `tag` without a matching `clientExtensions.tag` does not expand the TOPUP population beyond the existing cap population.
- Helper parity across `_build_open_exposure()`, `_execute_single_signal()`, and TOPUP on deterministic generated trade lists.
- Broker refresh after a successful order confirms the submitted pair-units change within tolerance and determines the next pair's actual count and net state.
- A successful order whose expected pair-units change is not visible blocks all remaining TOPUP candidates.
- Initial snapshot, post-fill snapshot, and malformed exposure failures never become empty exposure or permit additional orders.
- With the single-host witness enabled, a same-process TOPUP trade does not falsely block that cycle's normal entry, while a recent trade unknown to this process still blocks; witness errors fail closed.
- Dry-run submits no order and does not mutate simulated state as if a fill had occurred.

**Concurrency limitation:** runtime snapshots and the optional broker witness do not provide cross-host atomicity. If deployment requires a hard account-wide cap under concurrent hosts, implement and validate shared serialization as a separately approved prerequisite; otherwise state that the guard is sequential/best-effort.

### Stage 3 — Shadow evaluation

Run shadow mode for at least 7 consecutive calendar days. Review:

- would-block count/rate by reason, pair, and ladder rung;
- actual and would-be pair units / ladder depth, including whether steps past 2 units are routinely blocked;
- account fill representation (new trade per fill versus units merged into an existing trade);
- mismatch/refresh failure alerts and same-cycle normal-entry outcomes.

Shadow does not mitigate the P0 gap. The Owner reviews the report and explicitly chooses `off` or `enforce`; if the per-trade caps do not express the intended units/risk limit, define a separate units/risk cap instead of silently treating trade count as size.

### Stage 4 — Enforce (separate approved behavior-change commit)

In enforce mode, initialize from the same `open_trades` snapshot used for TOPUP aggregation, apply explicit cap arguments only after candidate qualification, and reconcile every successful order with a fresh `_all_open_trades_snapshot()` processed through `_strategy_exposure()`. Confirm the target pair's eligible net units changed by the expected signed units using `math.isclose(..., rel_tol=0, abs_tol=1e-6)`.

If refresh fails or does not show the expected change, stop the scan and emit a structured high-severity event/operator alert; do not only print and continue. Owner must accept the reduced ladder depth and confirm deployed account fill behavior before enabling enforcement.

If `SINGLE_HOST_GUARD_ENABLED` is true, apply its broker witness before TOPUP. It does not distinguish host, profile, or process, so track locally confirmed TOPUP trade IDs and let subsequent `_execute_single_signal()` checks exclude only those IDs. A second host has no such local allowlist and should still see the recent trade. Test both outcomes. This remains best-effort; hard cross-host caps require shared serialization.

### Stage 5 — Weak-exit ownership (only if B1 is approved)

Filter non-bot-owned trades from the weak-exit candidate list before support scoring, ranking, and `MIN_GROUP_SIZE`. Keep `_gated_close_trade_by_id()` as the live execution-time authorization check.

**Focused tests:**

- Manual trades do not affect a bot group's ranking or minimum size.
- Eligible bot trades still select the same weak candidate when manual trades are present.
- The close-time ownership gate remains in place.

### Stage 6 — OVERRIDE lifecycle (based on C decision)

- **C2 (recommended):** clarify the `_maintain_group_positions` and weak-exit comments/docstrings; add a focused regression test that OVERRIDE trades remain eligible for scale-out and weak-exit but retain the early-exit exemption.
- **C1:** add OVERRIDE exclusions to the selected scanning paths and test that ordinary trades are unaffected. Do not implement both C1 and C2 semantics.

### Stage 7 — P2 follow-up

Read-only inspect deployed effective units, dlot scale, prob-zone result, clamp floor, and final units; quantify how often floors undo shrinkage before assigning D a low risk. Make no sizing behavior changes in this patch. Keep the existing `BASE>2` startup warning for E and add a regression test if related code is touched; do not change cap semantics here.

## 4. Verification and rollout gate

1. Run focused tests for TOPUP, exposure, basket selection, weak-exit, scale-out, and the affected runner wiring.
2. Run the existing relevant test suite and Python syntax/compile check for changed Python files.
3. Review the final diff to ensure only approved paths and tests changed.
4. Exercise only mocked tests and dry-run validation; do not validate by placing an order on a live or practice account.
5. Before deployment, separately review the proposed behavior and confirm account/profile safeguards. Deployment approval is outside this coding plan.

**Completion criteria:** all approved decisions are implemented; cap modes have explicit tested semantics; helper parity holds; post-fill reconciliation is fail-closed and observable; targeted and regression tests pass; shadow impact and ladder-depth changes are recorded; review report and this plan record implementation status and deferred decisions.
