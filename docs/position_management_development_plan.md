# Position Management — Coding Development Plan

- Date: 2026-11-23
- Status: **PLAN · implementation awaits Owner confirmation**
- Source of truth: [position_management_review.md](position_management_review.md)
- Scope: `scheduled_runner_v3.py` position-management paths only; no live trading or configuration changes are authorized by this plan.

## 1. Goal and boundaries

Close the confirmed MC-TOPUP cap gap while keeping unrelated position behavior stable. Resolve the weak-exit ownership and OVERRIDE lifecycle decisions explicitly before changing those behaviors. P2 observations (strict shrink-only and boost hard ceiling) remain out of scope unless separately approved.

Implementation principles:

- Fail closed when exposure, position count, or post-fill state cannot be established.
- Use the same ownership and `tag_prefix` population as the existing exposure and position-cap logic.
- A broker fill is not assumed to add one trade: reconcile the broker state after each successful top-up before evaluating another pair.
- Keep dry-run and test paths incapable of submitting live orders.
- Do not change run.env, live lot sizes, or unrelated entry/exit policies in this work.

## 2. Decisions required before behavior changes

| Decision | Recommendation | If not approved |
|---|---|---|
| A — TOPUP cap enforcement | Enforce the current cycle's `CROSS_MAX_NET_PER_CCY` and `MAX_OPEN_POSITIONS` on every top-up; stop the remaining scan unless the fresh broker snapshot confirms the expected pair-units change. Treat TOPUP as outside the OVERRIDE entry bypass. When enabled, honor the existing single-host witness, but recognize it is best-effort, not an atomic lock. | Do not implement the cap behavior; leave the P0 gap explicitly open. If a hard account-wide limit across hosts is required, approve a shared serialization design before claiming that guarantee. |
| B — weak-exit ownership | B1: exclude non-bot-owned trades before ranking and group-size evaluation; retain close-time gate as defense in depth. | Preserve current ranking population and document that manual positions can affect bot exit ranking. |
| C — OVERRIDE exits | C2: preserve current behavior; OVERRIDE bypasses only the maintain-path early-exit gates, not weak-exit or scale-out. | If full exit immunity is intended, select C1 and test it before implementation. |

P2 items D/E are monitoring-only in this plan and must not be bundled into the P0 patch.

## 3. Staged implementation

### Stage 0 — Baseline and approval

1. Record the current branch/commit and run the existing focused MC-TOPUP and exposure tests.
2. Obtain Owner decisions A, B, and C; record the selected B/C behavior in the review document before implementation.
3. Keep the change limited to v3 and tests unless repository evidence shows a directly required shared helper.

**Exit criteria:** decisions are recorded; baseline tests pass or pre-existing failures are identified and isolated.

### Stage 1 — Compute cycle caps before TOPUP

Move the MC regime, `_MAX_OPEN_POSITIONS`, and dynamic `_cross_net_cap` calculations to after `_all_trade_pairs`, MC cache loading, and `_global_scores` are ready, but before `_mc_min_position_topup_scan_v31()`.

Preserve the calculations and logging; change ordering only. Check all dependencies at the new location and ensure basket selection still receives the same values.

**Focused tests:**

- TOPUP sees the current cycle's MC-selected max-position and cross-net caps, not values retained from the previous cycle.
- A cycle with no MC pairs retains the existing no-data/neutral behavior.
- Existing basket cap and override behavior remains unchanged.

### Stage 2 — Guard MC-TOPUP and reconcile each fill

1. Build TOPUP's eligible positions using the same `is_bot_owned_trade` plus configured `tag_prefix` filter used by `_build_open_exposure()` and `_execute_single_signal()`.
2. Initialize `_rt_net` and `_rt_open_count` from the exact `open_trades` snapshot TOPUP already fetched. Count every eligible broker trade individually; do not infer count from the deduplicated `held` set.
3. Before each instrument can submit:
   - Skip if `_rt_open_count >= _MAX_OPEN_POSITIONS`.
   - Simulate the one-trade directional delta for base and quote currencies; skip if either absolute net would exceed `_cross_net_cap`.
   - On malformed instrument/units or cap-evaluation errors, skip that instrument (fail-closed).
4. When `SINGLE_HOST_GUARD_ENABLED` is true, apply the existing broker witness before starting the TOPUP order sequence and fail closed on witness errors. This is best-effort only; it does not serialize two hosts that check concurrently.
5. After a successful order, fetch a fresh broker open-trade snapshot and verify the target pair's eligible net units changed by exactly the submitted signed units. Rebuild both runtime values using the same filters and counting rules. Do not increment the count solely because the trade call returned a truthy result.
6. If the post-fill read fails, the expected units change is not visible, or rebuilding fails, stop the remaining TOPUP scan; do not submit another pair using stale or unconfirmed exposure.
7. Preserve current SL/TP checks, regime/direction gates, ladder sizing, and dry-run behavior.

**Concurrency limitation:** runtime snapshots and the optional broker witness do not provide cross-host atomicity. If deployment requires a hard account-wide cap under concurrent hosts, implement and validate shared serialization as a separately approved prerequisite; otherwise state that the guard is sequential/best-effort.

**Focused tests (extend `tests/test_mc_min_position.py` or add a dedicated cap-guard test module):**

- Existing eligible trades at the max-position cap block TOPUP; one below the cap can proceed.
- A single top-up that would cross either currency's net cap is blocked; a boundary result equal to the cap is allowed, consistent with basket logic.
- Two different pairs cannot cumulatively exceed the same currency cap in one scan.
- Multiple trades on one instrument count individually, even though pair aggregation yields one TOPUP candidate.
- Bot-owned trades without a configured strategy prefix are excluded from both the runtime count and TOPUP aggregation.
- A top-level `tag` without a matching `clientExtensions.tag` does not expand the TOPUP population beyond the existing cap population.
- Broker refresh after a successful order confirms the submitted pair-units change and determines the next pair's actual count and net state.
- A successful order whose expected pair-units change is not visible blocks all remaining TOPUP candidates.
- Initial snapshot, post-fill snapshot, and malformed exposure failures never become empty exposure or permit additional orders.
- With the single-host witness enabled, TOPUP honors the existing preflight check and fails closed on check errors; tests/documentation do not claim atomic multi-host exclusion.
- Dry-run submits no order and does not mutate simulated state as if a fill had occurred.

### Stage 3 — Weak-exit ownership (only if B1 is approved)

Filter non-bot-owned trades from the weak-exit candidate list before support scoring, ranking, and `MIN_GROUP_SIZE`. Keep `_gated_close_trade_by_id()` as the live execution-time authorization check.

**Focused tests:**

- Manual trades do not affect a bot group's ranking or minimum size.
- Eligible bot trades still select the same weak candidate when manual trades are present.
- The close-time ownership gate remains in place.

### Stage 4 — OVERRIDE lifecycle (based on C decision)

- **C2 (recommended):** clarify the `_maintain_group_positions` and weak-exit comments/docstrings; add a focused regression test that OVERRIDE trades remain eligible for scale-out and weak-exit but retain the early-exit exemption.
- **C1:** add OVERRIDE exclusions to the selected scanning paths and test that ordinary trades are unaffected. Do not implement both C1 and C2 semantics.

### Stage 5 — P2 observation

Do not change the dlot floor, clamp floor, or ordinary boost ceiling in this patch. Record observed configurations and revisit only with a separate risk/behavior decision.

## 4. Verification and rollout gate

1. Run focused tests for TOPUP, exposure, basket selection, weak-exit, scale-out, and the affected runner wiring.
2. Run the existing relevant test suite and Python syntax/compile check for changed Python files.
3. Review the final diff to ensure only approved paths and tests changed.
4. Exercise only mocked tests and dry-run validation; do not validate by placing an order on a live or practice account.
5. Before deployment, separately review the proposed behavior and confirm account/profile safeguards. Deployment approval is outside this coding plan.

**Completion criteria:** all approved decisions are implemented; cap guards use current-cycle values and consistent per-trade counts; post-fill reconciliation is fail-closed; targeted and regression tests pass; review report and this plan record the implemented status and any deferred decisions.
