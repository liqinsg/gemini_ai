# Position Management: Stage Prompts

- Date: 2026-11-23
- Status: **FINAL (v1.1)**: TRAE table/version edits merged with the Stage 1 test, Stage 2b shadow-refresh and decisions-record amendments
- Companion to: `position_management_review.md` (v1.6/v1.7) and `position_management_development_plan.md`
- Target: `scheduled_runner_v3.py` position-management paths only

## How to use

- Run **one prompt per session**, in order.
- Prepend the **Shared preamble** to every stage prompt.
- Every stage ends with a stop-and-report step. Nothing rolls into the next stage on its own.

## Flags before you start

- **Stage 2a edits live entry paths** (`_build_open_exposure`, `_execute_single_signal`). It is split from the shadow work so the refactor can be verified as behavior-preserving on its own.
- **Stages 3 and 4 are gated on the Owner, not the agent.** The prompts forbid the agent from choosing `enforce` or skipping the 7-day shadow window.
- **Line numbers rot after Stage 1.** The prompts anchor on function names and tell the agent to re-locate by symbol.
- **Default must stay `off`** in every commit until enforcement is approved.
- **Shadow mode does not mitigate the P0 gap.** It only measures the impact of enforcing. The gap stays open for the whole shadow window.
- **Edits to `scheduled_runner_v3.py` may be live on save** if a scheduler runs the working tree. Stage 1 pre-checks this.

## Stage map

| Stage | Type                                      | Gate                                                              |
| ----- | ----------------------------------------- | ----------------------------------------------------------------- |
| 0     | Read-only baseline                        | none                                                              |
| 1     | Refactor (no behavior change)             | Stage 0 decisions recorded; Stage 1-only authorization            |
| 2a    | Refactor (no behavior change, live paths) | Stage 1 tests green and committed; separate Owner go-ahead        |
| 2b    | Switch-gated, default `off`               | Stage 2a parity test green; fill mode stated in decisions record  |
| 3     | Analysis only                             | at least 7 consecutive days of shadow logs                        |
| 4     | Behavior change                           | Owner approval of `enforce` in the decisions record               |
| 5     | Behavior change                           | Owner chose B1                                                    |
| 6     | Comments/tests (C2) or logic (C1)         | Owner chose C1 or C2                                              |
| 7     | Read-only measurement                     | none                                                              |

---

## Shared preamble (prepend to every stage)

```
CONTEXT
Repo: scheduled_runner_v3.py position-management work.
Source of truth: position_management_review.md (v1.7) and position_management_development_plan.md.
Work ONLY on the stage named below. Do not start later stages.

HARD RULES
- No live or practice-account orders from tests or from you. Mocked tests and dry-run only.
- Do not modify run.env, live lot sizes, or unrelated entry/exit policy.
- Fail closed: if exposure, count, or post-fill state can't be established, block/stop; never treat failure as empty exposure.
- Ownership population everywhere = is_bot_owned_trade + clientExtensions.tag matching a _strategy_groups tag_prefix. Matching is SUBSTRING containment (`p in raw_tag`), not startswith, exactly as the existing code does. No top-level `tag` fallback.
- Position count is per broker trade (+=1 each), never len(held).
- Caps are strategy-scoped, not account-wide. Don't describe them as account-risk limits.
- Anchor on function/symbol names, not line numbers; re-locate by symbol since lines shift.
- Keep behavior-changing and behavior-preserving changes in separate commits.
- Do not commit unless asked. If asked, stage explicit paths only (`git add <path>`), never `git add -A` or `git add .`.
- Do not touch files you were not asked to change, including pre-existing unrelated working-tree changes. Report them instead.
- Default for any new switch is OFF.

OUTPUT FORMAT (terminal, labeled sections)
[CHANGED] files + functions touched
[TESTS] added/modified, and run results
[RISKS] dangerous defaults or behavior changes spotted
[DEFERRED] anything out of scope you noticed
[NEXT] what needs Owner approval before the next stage
Then STOP.
```

---

## Stage 0: Baseline and approval

```
STAGE 0 (read-only, no code changes)
1. Record current branch + commit hash.
2. Run `git status --short` and `git diff --summary`; list any pre-existing uncommitted change (mode changes, deletions).
3. Run existing focused tests: MC-TOPUP, exposure, basket selection, single-host guard. List pass/fail; isolate pre-existing failures.
4. Read-only inspect how fills are represented for this account: what _trading_core.execute_market_trade returns, whether it can return tradeOpened vs tradeReduced, and whether the account is hedging or netting. Cite the code, not comments. If it cannot be established from code, say "unknown, needs a read-only broker check".
5. Read-only inspect the effective production sizing path (LOT_SIZE, dlot scale, prob-zone, clamp floor) and report the typical final units.
6. Draft a "Decisions record" block I can paste into the review doc (template at the end of this file), with blanks for the Owner's choice.
Do not edit any source file.
```

---

## Stage 1: Move cap computation and pass explicitly (no behavior change)

```
STAGE 1 (behavior-preserving refactor, one commit)
Goal: _mc_min_position_topup_scan_v31 must receive the current cycle's caps instead of reading stale module globals.

PRE-CHECKS (before editing)
- Run `git status --short` and `git diff --summary`. List any change not made by you (mode changes, deleted files such as the *_jcs*.py runners). Do not touch them. Report who imports them (grep) and whether any scheduler/cron/systemd unit executes the working-tree scheduled_runner_v3.py. If it does, state that edits are live on save and ask whether to work on a branch/copy instead.

CHANGES
1. In run_cycle, move the MC regime / _MAX_OPEN_POSITIONS / dynamic _cross_net_cap (boost + STRONG_GAP) computation to after _all_trade_pairs, MC cache load, and _global_scores are ready, but BEFORE the TOPUP call. Verify each dependency is available at the new location.
2. Add parameters cross_net_cap, max_open_positions, cap_guard_mode to the TOPUP function (defaults keep existing callers working). cap_guard_mode is accepted but unused here; the value passed is "off".
3. Preserve calculations and log strings exactly. Basket selection must receive identical values.

REQUIRED NEW TESTS (the stage is not complete without them; the existing suite does not exercise the reorder)
a. Call-order/args: with run_cycle's collaborators mocked, assert TOPUP is invoked AFTER the MC-regime/cap computation and receives cross_net_cap and max_open_positions equal to THIS cycle's values. Seed the module globals with deliberately different "previous cycle" values to prove they are not read.
b. No-MC-pairs: empty _all_trade_pairs -> _global_mc == "NO_MC_DATA", max positions == MC_NEUTRAL_MAX_POSITIONS, cross cap == base, and TOPUP is still called with those values.
c. Basket parity: _pick_global_basket sees the same _cross_net_cap / _MAX_OPEN_POSITIONS values as before the move (compare against a reference computed with the old ordering), including the OVERRIDE caps.
d. Failure mode: if the MC block raises, document and assert the resulting behavior (TOPUP is skipped; before the move TOPUP had already run). State this difference in [RISKS].

REPORT
- Note in [RISKS]: console log order changes ([GLOBAL MC]/net-cap lines now print before [MC-TOPUP]); Stage 3 log parsing must not assume the old order.
Do NOT add the helper, shadow logging, or any cap checks in this commit.
```

---

## Stage 2a: Shared exposure helper (behavior-preserving)

```
STAGE 2a (behavior-preserving refactor, separate commit; touches live entry paths, so be conservative)
1. Add _strategy_exposure(trades) -> (net, count, eligible), with exact ownership filtering: is_bot_owned_trade plus clientExtensions.tag SUBSTRING match (`p in raw_tag`) against the _strategy_groups tag_prefix values. Do not use startswith. net uses s=±1 on base/quote per trade; count is += 1 per eligible trade.
2. Refactor _build_open_exposure() (derive `held` from eligible) and _execute_single_signal() (_strategy_open, same-pair checks) to use it. Preserve each caller's existing fail-closed behavior on malformed records.
3. Add a deterministic parity test using seeded generated trade lists: manual tags, bot-owned with unmatched prefix, tags where the prefix appears mid-string (substring case), multiple trades per pair, long/short, malformed records. Assert old logic (copy the pre-refactor logic into the test as a reference oracle) == helper output. Follow the repo's pytest convention.
4. Do NOT touch MC-TOPUP in this commit.
Report any discrepancy between old paths you discover. Do not "fix" it silently.
```

---

## Stage 2b: Shadow instrumentation in MC-TOPUP

```
STAGE 2b (switch-gated, default OFF)
PREREQUISITE: the decisions record must state the account fill mode (hedging/netting) or explicitly "unknown, learn from shadow logs". If unknown, say so in [RISKS].

1. Add validated env/config MC_TOPUP_CAP_GUARD = off|shadow|enforce. Invalid value -> startup error or fall back to off with a loud warning. Default off.
2. TOPUP: aggregate eligible positions via _strategy_exposure (same population as cap logic). Initialize runtime net/count from the SAME open_trades snapshot TOPUP already fetched.
3. Place the cap check immediately before the dry-run/order decision, after regime, direction, ladder, and SL/TP gates pass, so only real would-submit candidates are evaluated.
4. Modes:
   - off: no change in behavior; no extra broker reads.
   - shadow: compute the would-block decision and emit one structured log line (reason, instrument, rung, add_units, caps, runtime net/count). Do NOT block.
   - enforce: block on exceeding either currency's net cap (equality allowed) or count >= max_open_positions. Cap-evaluation exceptions skip that pair; init failure aborts the whole scan. The mode is never switched automatically.
5. Shadow-mode observation of real fills (shadow orders are real, so Stage 3 needs trustworthy data):
   - After each LIVE TOPUP order that returns success, take a read-only fresh snapshot (_all_open_trades_snapshot) and run it through _strategy_exposure.
   - Log one structured line: instrument, submitted signed units, expected pair net, observed pair net, observed eligible count before/after, and whether the fill looks like a new trade or units merged into an existing trade.
   - Use the refreshed state (not an assumed +1) as the simulated state for the next pair's would-block evaluation. If the refresh fails, log it and stop shadow evaluation for the rest of the scan; DO NOT block or alter the live order flow.
   - Compare with math.isclose(rel_tol=0, abs_tol=1e-6).
   - Observation only: never blocks, retries, or changes live behavior.
6. Tests: per-mode semantics; logs only for fully qualified candidates; boundary cases (equality with cap allowed); two pairs can't cumulatively exceed one currency's cap in a sequential scan; multi-trade-per-pair counts individually; unmatched-prefix and top-level-tag trades excluded; dry-run mutates no simulated state; shadow refresh uses post-fill state for the next pair; shadow refresh failure never blocks or changes order flow; off mode performs no extra broker reads.
Do NOT implement enforcement blocking against real orders beyond the tested enforce branch's default-off wiring, the alert/halt-on-mismatch behavior, or the single-host witness changes here (Stage 4).
```

---

## Stage 3: Shadow evaluation (analysis only)

Run after at least 7 consecutive calendar days of shadow data.

```
STAGE 3 (analysis script + report; no trading-code changes)
Input: shadow logs from MC_TOPUP_CAP_GUARD=shadow, <path/date range>.
Write a read-only script that reports:
- would-block count/rate by reason, pair, and ladder rung
- actual vs would-be pair units / ladder depth; how often steps beyond 2 units are blocked
- fill representation seen (new trade per fill vs merged into an existing trade), from the shadow refresh lines
- any refresh/mismatch events and same-cycle normal-entry outcomes
Account for the log-order change introduced in Stage 1 when parsing.
Verify the window covers >=7 consecutive calendar days; if not, say so and stop.
End with: neutral summary + the question for the Owner: "Is reduced ladder depth intended, or is a separate units/risk cap needed?" Do NOT recommend flipping to enforce.
```

---

## Stage 4: Enforce, reconciliation, single-host witness

Needs explicit Owner approval of `enforce`.

```
STAGE 4 (behavior change, separate commit. Proceed only if the Owner approved enforce in the decisions record: <paste it>)
If the record is missing or says "off", STOP.
1. After each successful TOPUP order, take a fresh _all_open_trades_snapshot(), run it through _strategy_exposure, and verify the target pair's eligible net units changed by the exact signed submitted units (math.isclose, rel_tol=0, abs_tol=1e-6). Rebuild runtime net/count from this snapshot.
2. On refresh failure, parse failure, or missing expected change: stop the remaining scan and emit a structured high-severity event / operator alert (not just print).
3. SINGLE_HOST_GUARD_ENABLED: run the existing witness once before the TOPUP order sequence; fail closed on witness error. Track locally confirmed TOPUP trade IDs so later _execute_single_signal checks exclude only those IDs. A recent trade not in the local list must still block.
4. Tests: mismatch/refresh-failure halts the scan; post-fill state drives the next pair's decision; same-process TOPUP trade doesn't block this cycle's normal entry; unknown recent trade still blocks; witness error fails closed.
5. Do not claim cross-host atomicity anywhere in code comments or logs. State best-effort.
Keep the default mode off.
```

---

## Stage 5: Weak-exit ownership (only if B1 approved)

```
STAGE 5 (only if the Owner chose B1; otherwise STOP)
In _weak_exit_scan_v31, exclude non-bot-owned trades before support scoring, ranking, and MIN_GROUP_SIZE evaluation. Keep _gated_close_trade_by_id as the execution-time check. Update the stale "bot account doesn't open manually" comment.
Tests: manual trades don't change a bot group's ranking or minimum size; the same weak candidate is chosen with and without manual trades present; the close gate still rejects non-bot-owned closes.
```

---

## Stage 6: OVERRIDE lifecycle (per C decision)

```
STAGE 6 (Owner decision: <C1 or C2>)
If C2: update comments/docstrings (maintain path, weak-exit, config) to state that OVERRIDE is exempt only from the maintain-path early-exit gates, not from weak-exit or scale-out. Add a regression test asserting OVERRIDE trades remain eligible for scale-out and weak-exit while keeping the early-exit exemption. No logic change.
If C1: add OVERRIDE exclusion to the chosen scan paths, with tests that ordinary trades are unaffected and OVERRIDE trades are skipped.
Never implement both semantics.
```

---

## Stage 7: P2 follow-up (read-only)

```
STAGE 7 (measurement only; no sizing changes)
Using logs or a dry-run harness, quantify how often the dlot floor fallback and clamp floor undo shrinkage: report distributions of dlot_scale, prob-zone units, and final units. Confirm that the existing BASE>2 startup warning (E) exists and add a regression test for its log only if related code is touched.
Report whether D's "low risk" rating holds given effective production LOT_SIZE.
```

---

## Decisions record (paste into the review doc after Stage 0)

```
A  TOPUP cap guard:   [ off | shadow | enforce ]   Owner: ____  Date: ____
   Reduced ladder depth accepted:  [ yes | no ]
   Hard cross-host cap required:   [ yes | no ]  (if yes, shared serialization is a separate prerequisite)
B  weak-exit ownership:  [ B1 | keep current ]     Owner: ____  Date: ____
C  OVERRIDE exits:       [ C1 | C2 ]               Owner: ____  Date: ____
D/E                      monitoring only, no code change
Shadow meaning:          [ same process, never blocks, live TOPUP keeps running | separate shadow instance ]
P0 gap during shadow:    acknowledged OPEN for the whole window (shadow mitigates nothing)   Owner: ____
Account fill mode:       [ hedging | netting | unknown, learn from shadow logs ]
Scheduler reads working tree:  [ yes | no ]   (if yes, edits are live on save)
Stage authorization:     Stage 1 only; Stage 2a needs a separate go-ahead
```
