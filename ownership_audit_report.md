# Ownership Boundary Adversarial Audit — `gemini_api`

**Audit type:** READ-ONLY static analysis. No files were modified, created, or deleted. No bot, script, or test was executed. No broker or network calls were made.
**Date:** 2026-09-29
**Scope:** Whole repository (runner, adapters, core, scripts, tools, tests).

> Every conclusion below is cited as `file:line` and is derived from static reading of the cited code.
> Where evidence was insufficient, the result is recorded as **UNKNOWN** rather than rounded up to PASS.

---

## Invariant Under Test

> The bot may mutate broker state **ONLY** for trades proven bot-owned by **CURRENT broker evidence** + a **concrete `trade_id`**.
> `instrument` = **discovery only**.
> Cached `trade_id`, strategy tag intent, and risk-engine intent are **NOT authorization**.

**Active runner:** `scheduled_runner_v3.py` — `RUNNER_VERSION = "3.0.0"` (`scheduled_runner_v3.py:462`), entrypoint `if __name__ == "__main__": run_cycle()` (`scheduled_runner_v3.py:1682-1683`).

**Authorization primitive:** `is_bot_owned_trade()` at `utils/utils.py:34-43`.

```python
BOT_OWNED_TAG_ROOT = "GEMINIAIBOT_V3"   # utils/utils.py:15

def is_bot_owned_trade(trade: dict) -> bool:      # utils/utils.py:34
    tag = str(
        trade.get("tag")
        or trade.get("clientExtensions", {}).get("tag")
        or ""
    )
    return tag.startswith(BOT_OWNED_TAG_ROOT)     # utils/utils.py:43
```

**Key structural point:** `is_bot_owned_trade` inspects whatever dict the caller hands it. Whether that dict is a *live broker snapshot* is determined entirely by the caller. This distinction drives the scenarios below.

---

## A. Mutation Surface

### Table 1 — Runner-reachable (`scheduled_runner_v3.py`)

| file:line | caller | API | direct/indirect | gated Y/N |
|---|---|---|---|---|
| `scheduled_runner_v3.py:195-197` | `_gated_close_trade_by_id` → `TradingCore.close_trade_by_id` | TradeClose | indirect | **Y** |
| `scheduled_runner_v3.py:223-225` | `_gated_update_sl_by_id` → `update_trade_sl_only` | TradeCRCDO | indirect | **Y** |
| `scheduled_runner_v3.py:254-261` | `_gated_attach_sltp_by_id` → `attach_sl_tp_to_trade_id` | TradeCRCDO | indirect | **Y** |
| `scheduled_runner_v3.py:295-299` | `_close_bot_trades_for_instrument` | TradeClose | indirect (via gate) | **Y** |
| `scheduled_runner_v3.py:345-350` | `_TradingCoreRiskAdapter.send_close_order` (Risk Engine) | TradeClose | indirect (via gate) | **Y** |
| `scheduled_runner_v3.py:352-358` | `_TradingCoreRiskAdapter.update_trade_sl` (Risk Engine) | TradeCRCDO | indirect (via gate) | **Y** |
| `scheduled_runner_v3.py:530-534` | `_emergency_close_all` | TradeClose | indirect (via gate) | **Y** |
| `scheduled_runner_v3.py:635-643` | `_sltp_guardian` | TradeCRCDO | indirect (via gate) | **Y** |
| `scheduled_runner_v3.py:1420-1422` | `_maintain_group_positions` — OVERRIDE early-exit | TradeClose | indirect | **Y** |
| `scheduled_runner_v3.py:1469-1471` | `_maintain_group_positions` — early-exit | TradeClose | indirect | **Y** |
| `scheduled_runner_v3.py:1255-1265` | `_execute_single_signal` → `execute_market_trade` | OrderCreate | indirect | **N** (scope) |

### Table 1b — Underlying primitives with NO ownership check inside

| file:line | API | note |
|---|---|---|
| `utils/trading_core_v2.py:421-446` | TradeClose | `close_trade_by_id`, no ownership check |
| `utils/trading_core_v2.py:448-480` | TradeCRCDO | `update_trade_sl_only`, no ownership check |
| `utils/trading_core_v2.py:151-201` | TradeCRCDO | `attach_sl_tp_to_trade_id`, no ownership check |
| `utils/trading_core_v2.py:373-419` | **PositionClose (position-level)** | `close_position`, no ownership check |
| `utils/trading_core_v2.py:103-149` | TradeCRCDO on `trade_ids[0]` | `attach_sl_tp_to_open_trade` — **"first trade" selection** |
| `utils/trading_core_v2.py:610-617` | PositionClose via `close_position` | `close_pair_position` — **instrument-level** |

### Reachability finding (material)

The v3 runner imports `TradingCore` from **`utils.trading_core_v2`** (`scheduled_runner_v3.py:109`), **not** from `utils.trading_core`.
The v144 / v145 / v2 runners import from `utils.trading_core` (`scheduled_runner_v145.py:93-94`, `scheduled_runner_v144.py:323-324`, `scheduled_runner_v2.py:84-86`).

Whether `utils/trading_core.py` re-exports `trading_core_v2` was **not verified** → this determines whether the legacy ungated PositionClose paths and the v3 gated paths share one implementation. **UNKNOWN.** (The v144/v145 code paths are verified ungated regardless of which module object they bind to.)

**`attach_sl_tp_to_open_trade` is NOT reachable from `scheduled_runner_v3.py`.** Verified by grep: v3 imports only `TradingCore` + `get_candles` (`scheduled_runner_v3.py:31,109`). The `trade_ids[0]` "first trade" selection at `utils/trading_core_v2.py:125` is reachable only from `scheduled_runner_v144.py:804`, `scheduled_runner_v145.py:579`, `scheduled_runner_v2.py:489`, `patches/*`, `tests/*`.

### Why the gate exists

Each `_gated_*` wrapper re-reads the trade by id and re-verifies ownership **before** delegating to `TradingCore`:

```python
def _gated_close_trade_by_id(trade_id, *, client_request_id, gate_name="GATE") -> bool:
    trade = _find_open_trade_by_id(trade_id)          # scheduled_runner_v3.py:180
    if trade is None: ... return False                # :181-186
    if not is_bot_owned_trade(trade): ... return False # :187-194
    return _trading_core.close_trade_by_id(...)       # :195-197
```

`_find_open_trade_by_id` (`scheduled_runner_v3.py:146-155`) issues a **live** `OpenTrades` call via `_trading_core.get_all_open_trades()` (`:149`) — not cached state.

**Minor cosmetic divergence:** the `_gated_*` error messages read only `trade.get("clientExtensions", {}).get("tag")` (`:188, 216, 247`), while `is_bot_owned_trade` also accepts a top-level `trade["tag"]` key (`utils/utils.py:38-42`). The gate decision is unaffected; only the diagnostic string can differ.

### Table 2 — Scripts / tools / tests

| file:line | API | reachable from v3 runner Y/N |
|---|---|---|
| `close_all.py:64-65` | OrderCancel (pending orders) | **N** — standalone script |
| `close_all.py:124-125` | PositionClose | **N** — standalone |
| `close_all_v1.py:98-99` | OrderCancel | **N** — standalone |
| `close_all_v1.py:158-159` | PositionClose | **N** — standalone |
| `close_all_runner.py:10-12,51-58` | imports v144/jcs emergency closers | **N** — standalone |
| `scheduled_runner_v144.py:426-427` | PositionClose (emergency) | **N** — separate runner |
| `scheduled_runner_v144.py:478-479` | PositionClose (`_close_pair_position_v144`) | **N** — separate runner |
| `scheduled_runner_v145.py:201-202` | PositionClose (emergency) | **N** — separate runner |
| `scheduled_runner_v145.py:253-254` | PositionClose (`_close_pair_position_v144`) | **N** — separate runner |
| `scheduled_runner_v1441.py:451` | `close_position` (emergency) | **N** — separate runner |
| `scheduled_runner_v1441.py:494` | `close_position` (`_close_pair_position_v144`) | **N** — separate runner |
| `scheduled_runner_jcs_claude.py:182-183` | PositionClose | **N** — standalone |
| `scheduled_runner_jcs_claude.py:250-251` | PositionClose | **N** — standalone |
| `scheduled_runner_jcs_claude.py:366-367` | PositionClose | **N** — standalone |
| `utils/oanda_execution.py:165,219` | OrderCreate | **N** — no v3 import found |
| `utils/oanda_execution.py:268` | OrderCancel | **N** |
| `utils/oanda_execution.py:277,293,309` | OrderCreate (SL/TP orders) | **N** |
| `utils/oanda_execution.py:351` | PositionClose (`close_all_trades`) | **N** |
| `utils/oanda.py:73-75,920-962,1037+` | OrderReplace / StopLossOrder / close | **N** — legacy adapter |
| `patches/scheduled_runner_jcs_v2.py:24,251` | `attach_sl_tp_to_open_trade` | **N** — `patches/` not imported by v3 |
| `patches/utils_safety_helpers.py:165,216` | `attach_sl_tp_to_open_trade` | **N** — `patches/` not imported by v3 |
| `tests/test_scheduled_runner_v144.py:62-63,92-93,102-114` | OrderCreate / TradeCRCDO / PositionClose | **N** — test, live-account roundtrip |

### Partial-unit close

`TradeClose` is sent with `{"units": "ALL"}` (`utils/trading_core_v2.py:429`). `update_trade_sl_only` and `attach_sl_tp_to_trade_id` send stopLoss-only (`:464-466`) or SL+TP (`:183-186`) payloads.

No partial-unit close path exists in the v3-reachable core. Partial close by ratio exists only in `utils/pyramid_cluster.py:436-475` (FIFO) and is exercised from `utils/risk_integration.py` per `tests/test_risk_integration.py:171-189` — **not** wired into `scheduled_runner_v3.py` (v3 uses `risk_engine_v22`, `scheduled_runner_v3.py:26-30`).

### Pending-order cancel / replace

**No `OrderCancel` / `OrderReplace` exists anywhere in the v3-reachable set** (verified by repo-wide grep). The only hits are `close_all.py:64`, `close_all_v1.py:98`, `utils/oanda_execution.py:268`, `utils/oanda.py`.

So there is no pending-order cancel/replace to gate — but equally, **no ownership gate on pending orders at all** (relevant to C6).

---

## B. Scenarios

### Scenario 1 — Bot trade A + manual trade B, same instrument: exit A must not touch B → **PASS**

Exits are `trade_id`-scoped, not instrument-scoped. `_close_bot_trades_for_instrument` enumerates via `_get_bot_trades_for_instrument` (`:130-143`), which filters on both instrument **and** ownership:

```python
return [
    t for t in all_trades
    if t.get("instrument") == instrument and is_bot_owned_trade(t)   # :141-142
]
```

Each element is then closed by concrete id through the gate (`:288-299`). Manual B never enters the list. The authoring comment at `:272-279` explicitly records the replacement of a former position-level flat-close.

> **Residual:** the ungated `TradingCore.close_trade_by_id` (`utils/trading_core_v2.py:421`) would close B if handed B's id — but no v3 caller derives an id from instrument alone.

### Scenario 2 — Bot BUY + manual SELL: manual never becomes bot-owned by instrument/direction match → **PASS**

Ownership is tag-only (`utils/utils.py:34-43`); instrument and direction are **never** inputs to the ownership decision. Manual SELL carries no `GEMINIAIBOT_V3` tag → excluded at `:520` (emergency), `:590` (guardian), `:1028` (exposure), `:1212` (entry), `:1301` (maintain).

### Scenario 3 — Same, net = 0: bot exposure stays visible; no netting used as authorization → **PASS (static)**

`_build_open_exposure` (`:1013-1042`) skips non-bot trades **before** any arithmetic (`:1028-1029`), then accumulates per trade:

```python
units = float(t.get("currentUnits", 0))     # :1036
s = 1 if units > 0 else -1                  # :1037
net[base] = net.get(base, 0) + s            # :1038
net[quote] = net.get(quote, 0) - s          # :1039
held.add((inst, s))                         # :1040
```

A manual opposite position cannot zero out the exposure because it is dropped first. Authorization never consults `net`.

> Runtime confirmation on a hedging account → **UNKNOWN** (see C5).

### Scenario 4 — Two bot trades, same instrument: distinct `trade_id`s kept; no "first trade" selection → **PASS**

`_get_bot_trades_for_instrument` returns a **list** (`:140-143`). `_close_bot_trades_for_instrument` loops `for t in bot_trades` deriving `trade_id` per element (`:288-289`). The SL/TP guardian likewise derives `cid` per trade (`:608`) and passes it explicitly (`:635-643`).

The "first trade" pattern exists only at `utils/trading_core_v2.py:125` (`trade_ids[0]`), which is **not reachable from v3**.

> Note: the v3 entry path deliberately **blocks** a second bot trade on the same pair (`:1231-1236`), so this scenario is largely defensive. The code does not collapse to one id.

### Scenario 5 — Risk Engine emits a manual `trade_id`: broker state re-read, ownership revalidated, rejected, no mutation → **PASS**

The adapter's mutators route into the gates:

```python
def send_close_order(self, trade_id, client_request_id) -> bool:
    return _gated_close_trade_by_id(..., gate_name="RISK-ENGINE")   # :345-350

def update_trade_sl(self, trade_id, new_sl, client_request_id) -> bool:
    return _gated_update_sl_by_id(..., gate_name="RISK-ENGINE")     # :352-358
```

Each gate re-fetches **live** `OpenTrades` (`_find_open_trade_by_id`, `:149`) and re-checks `is_bot_owned_trade` (`:187, 215`), returning `False` with **no broker call** on failure.

Risk-engine call sites: `risk_engine_v22.py:334` (`update_trade_sl`) and `risk_engine_v22.py:250` (`broker.send_close_order`).

> **UNKNOWN sub-case:** if `_trading_core.get_all_open_trades()` raises, `_find_open_trade_by_id` swallows the exception (`:153-154`) and returns `None` → the gate prints `"trade not in OpenTrades (already closed?)"` (`:182-186`) and returns `False`. Fail-closed, but a fetch failure is misreported as a closure.

### Scenario 6 — SL/TP Guardian meets a manual trade: excluded before any mutation → **PASS**

`_sltp_guardian` skips non-bot-owned **first** (`:590-592`), counts them (`:591`), and only mutates via the gate (`:635-643`).

### Scenario 7 — `_emergency_close_all` does not bypass ownership → **PASS**

```python
for trade in all_trades:                              # :515
    if not is_bot_owned_trade(trade):
        result["skipped_manual"] += 1
        continue                                      # :520-522
    ...
    ok = _gated_close_trade_by_id(trade_id, ...)      # :530-534
```

The docstring claim at `:503-504` ("Emergency is NEVER a license to bypass the ownership filter") matches the code.

### Scenario 8 — Restart: ownership rebuilt purely from broker-side tag, no local memory → **PASS**

Every ownership decision reads the tag off a freshly fetched broker record: `get_all_open_trades()` issues `OpenTrades` (`utils/trading_core_v2.py:79-83`) and callers pass those dicts to `is_bot_owned_trade`. No local ownership cache exists in the v3 path.

The one local file is `risk_state.json` (`scheduled_runner_v3.py:373-374`), consumed by risk-engine state / pending-exits (`risk_engine_v22.py:266-269`) — it is **never** consulted for ownership.

### Scenario 9 — Fresh fill returns trade WITHOUT expected tag: must become UNKNOWN, not managed, no SL/TP or close by assumption, discrepancy surfaced → **FAIL**

This scenario is **not implemented**, and the code resolves the opposite way.

1. `execute_market_trade` sets both extension keys — correct:
   ```python
   "clientExtensions": client_extensions or {},        # trading_core_v2.py:267
   "tradeClientExtensions": client_extensions or {},   # trading_core_v2.py:268
   ```
2. It then GETs the trade by id (`:308-313`) and verifies **only SL/TP presence** (`:315-321`), using that GET purely to decide whether to re-attach SL/TP (`:323-344`).
3. **No verification that the returned trade carries `GEMINIAIBOT_V3`.**
4. An untagged fill is treated as normal success: the function returns `True` (`:367`) and, if SL/TP were missing, proceeds to re-attach via `attach_sl_tp_to_trade_id` (`:325-332`) — **a mutation** — with no ownership check anywhere on that path.
5. **No discrepancy is surfaced.** No log, counter, or alert reports "tag missing". The only related logging is the SL/TP message at `:300-302`.
6. The untagged trade is never added to a managed set (there is no persisted managed set — it is re-derived each cycle at `:1023, 1207, 1283`), so the next cycle's ownership filter silently drops it.

**Consequence:** the trade does not "become UNKNOWN"; it becomes **permanently unmanaged** *and* may have received an SL/TP attach while unowned. Required behaviour (UNKNOWN state + surfaced discrepancy) is absent.

### Scenario 9b — Entry path is not gated and does not verify ownership → **UNKNOWN (scope)**

`OrderCreate` (`utils/trading_core_v2.py:274`) is not behind any `_gated_*` wrapper. The pre-entry checks (`:1207-1236`) are exposure/idempotency checks built on bot-owned trades; they gate *entry*, not ownership of the resulting position.

Whether opening a new position falls inside "mutating existing broker state" is a scope decision that is not defined by the code or config → **UNKNOWN**, not PASS.

### Scenario 9c — `tradeReduced` path adopts an existing trade's id → **FAIL**

```python
if fill.get("tradeOpened"):
    trade_id = fill["tradeOpened"].get("tradeID")    # :283-284
elif fill.get("tradeReduced"):
    trade_id = fill["tradeReduced"].get("tradeID")   # :285-286
```

A `tradeReduced` transaction refers to an **existing** position being reduced. That id is then used at `:325-332` for a possible SL/TP attach with **no ownership check**. Same root cause as Scenario 9.

---

## C. Tag Propagation and Account Mode

### C1 — Does `OrderCreate` set `tradeClientExtensions`, or only `clientExtensions`?

**Both.** `utils/trading_core_v2.py:267-268` sets `clientExtensions` **and** `tradeClientExtensions` to the same dict. This is what makes the tag land on the **Trade** object (as opposed to only the **Order**).

> **Contrast:** `utils/oanda_execution.py:151,212` set **only** `clientExtensions`, never `tradeClientExtensions` — trades opened through that adapter would **not** carry the tag on the Trade. Not reachable from v3, but a real propagation hole in the repo.

### C2 — Where is the tag created, attached, and later read?

| Stage | Location |
|---|---|
| **Created** | `make_strategy_tag` → `{prefix}_{pair}_{SIDE}_{YYYYMMDD}` then wrapped with root via `_tag_with_bot_root` (`utils/utils.py:176-181`, `:19-23`), producing `GEMINIAIBOT_V3::JPY-STRENGTH_...`. Called at `scheduled_runner_v3.py:1240` |
| **Suffix** | `_OVERRIDE` / `_MCSEV` / `_MCMOD` appended at `scheduled_runner_v3.py:1241-1246` |
| **Attached** | `build_client_extensions(...)` → `client_extensions` (`scheduled_runner_v3.py:1262-1264`) → both extension keys (`utils/trading_core_v2.py:267-268`). The root wrap is applied again defensively and is idempotent (`utils/oanda_state.py:59`, `utils/utils.py:21-22`) |
| **Read from Trade** | `is_bot_owned_trade` (`utils/utils.py:38-43`); `is_strategy_trade` strips the root via `_extract_raw_tag` (`utils/utils.py:188-197`, `:26-31`) |

### C3 — Is there post-fill GET `OpenTrades` verification BEFORE the trade enters the managed set?

**Partially, and not for ownership.**

A post-fill GET exists — `TradeDetails` at `utils/trading_core_v2.py:308-313` — but it verifies **SL/TP only** (`:315-321`).

There is no managed *set* to enter: the runner re-derives bot-owned trades from a fresh `OpenTrades` each cycle (`scheduled_runner_v3.py:1023, 1207, 1283`). A per-trade GET-`OpenTrades` ownership confirmation before management does **not** exist. → **UNKNOWN / absent.**

### C4 — Behavior for tag missing / `None` / malformed / wrong root?

| Input | `is_bot_owned_trade` (`utils/utils.py:38-43`) | `is_strategy_trade` (`utils/utils.py:188-197`) |
|---|---|---|
| Missing / `None` | `str(... or "")` → `""` → `startswith` **False** → non-owned | False |
| Wrong root (`GEMINIAIBOT_V2::...`) | **False** → non-owned | False |
| Malformed but prefixed (`GEMINIAIBOT_V3BAD...`) | **True** → treated as bot-owned | **False** (strict `::` strip) |

**Net effect:** a malformed-root tag can pass the *ownership* gate while failing the *strategy-group* filter — i.e. bot-owned-but-ungrouped.

**Divergence worth flagging:** `scheduled_runner_v3.py:524, 595` use **substring** matching (`any(pfx in raw_tag ...)`) while `utils/utils.py:197` uses **`startswith`**. This inconsistency is not itself an ownership bypass, but it means group membership is decided differently in different code paths.

### C5 — Hedging or netting/FIFO assumed? Can a manual opposite order close a bot trade at the broker without any bot code path? Detected?

**No account mode is ever queried.** Repo-wide grep for `HEDGING|hedgingEnabled|netting|FIFO` finds no account-mode check anywhere. The only `FIFO` references are `utils/pyramid_cluster.py:132,436-475`, describing OANDA's FIFO rule.

The code assumes **hedging** semantics implicitly — it treats per-trade `currentUnits`, per-trade `tradeIDs` under `long`/`short`, and per-trade closes as independent. `utils/position_direction.py:66-67` even comments *"not expected for this non-hedging swing-trend strategy"* while still handling the hedged case.

> **This is the sharpest finding in the audit.**
> On a **netting / FIFO** account, a manual opposite-direction order can reduce or close the bot's trade **at the broker with no bot code path involved**. The bot cannot prevent this and **does not detect it** — there is no reconciliation comparing the bot's expected trade set against `OpenTrades` and reporting unauthorized disappearance. The only closure detection is `query_trade_raw` returning `CLOSED` (`utils/trading_core_v2.py:546-558`), reached only when the bot itself already decided to exit that id.

→ **UNKNOWN** (account mode never verified) **+ FAIL** for "detected?".

### C6 — Do pending orders get the same ownership gate as trades?

**No.** Pending orders are consulted only for idempotency, by tag match (`utils/utils.py:218-230`; `scheduled_runner_v145.py:293-306`; `scheduled_runner_v144.py:524-527`).

There is no ownership gate on pending orders because there are **no pending-order mutations** in the v3 path (no `OrderCancel`/`OrderReplace` — verified by grep).

**Asymmetry to note:** `utils/utils.py:225` uses `tag.startswith(prefix)` with **no** `_extract_raw_tag`. A properly root-wrapped pending order (`GEMINIAIBOT_V3::JPY-STRENGTH_...`) would **not** match `tag.startswith("JPY-STRENGTH")` — a false negative in duplicate-entry prevention, not an ownership bypass. v3 itself does not call this function (grep shows `check_pair_level_strategy_position` only in `scheduled_runner_v2.py:785`); v3 re-implements the check with root-stripping at `:1214-1216`.

---

## D. Legacy `JPY-STRENGTH_*` — Current Behavior Only

Applies to `scheduled_runner_v145.py` / `scheduled_runner_v144.py` / `scheduled_runner_v2.py`.

Their `_is_jpy_strength_trade` uses raw `startswith("JPY-STRENGTH")` (`v145:273-276`, `v144:499-502`) — the root-wrapped modern tag format would **not** pass, so legacy filters match only unwrapped legacy tags.

| Concern | Managed Y/N | file:line |
|---|---|---|
| Ownership filter | **N** — tag is `JPY-STRENGTH*`, not the `GEMINIAIBOT_V3` root; no `is_bot_owned_trade` import | `v145:273-276`, `v144:499-502`, `v2:413-415` |
| Exposure | **N** — no bot-owned exposure builder | absent (no equivalent of `scheduled_runner_v3.py:1013-1042`) |
| Duplicate detection | **Y** (legacy form) | `v145:279-309`, `v144:505-533`, `v2:785-786` via `utils/utils.py:200-237` |
| Grouping | **N** | — |
| SL/TP guardian | **Y** but **ungated, position-level** | `v144:804`, `v145:579` → `attach_sl_tp_to_open_trade` → `utils/trading_core_v2.py:114-125` (`trade_ids[0]`) |
| Risk engine | **N** | — |
| Emergency close | **Y** but **ungated, instrument-level** | `v144:426-427,478-479`, `v145:201-202,253-254` (PositionClose); `v144:922-923`, `v145:695-696` call it |
| Early exit | **Y** but **ungated, position-level** | `v144:870-871`, `v145:644-645` → `_close_pair_position_v144` |
| Reconciliation | **N** | — |

---

## E. Verdict

### FAIL

1. **Scenario 9 — untagged fill is not detected.** `utils/trading_core_v2.py:308-344` verifies SL/TP only; no post-fill ownership read; no discrepancy surfaced. Required UNKNOWN state does not exist. (`:315-321, 364-367`)
2. **Ungated mutation on a trade of unproven ownership** (same root cause). `utils/trading_core_v2.py:325-332` re-attaches SL/TP to the post-fill `trade_id` with no ownership check. Reached from the runner via `scheduled_runner_v3.py:1255` → `execute_market_trade`, and from `:334-336` when `trade_id` is falsy.
3. **`tradeReduced.tradeID` path.** `utils/trading_core_v2.py:285-286` adopts an *existing* trade's id from a reduce transaction and may then attach SL/TP to it at `:325-332`. Same missing ownership check.
4. **Account mode never verified; no unauthorized-closure detection.** No account-mode query anywhere in the repo. On a netting/FIFO account a manual opposite order can close a bot trade entirely outside bot code, undetected. (`utils/trading_core_v2.py:79-83, 546-558`; `utils/position_direction.py:66-67`)
5. **Legacy runners mutate position-level, ungated, and remain present and runnable.** `scheduled_runner_v144.py:426-427,478-479,804`; `scheduled_runner_v145.py:201-202,253-254,579`; `scheduled_runner_v2.py:489,579`; `scheduled_runner_v1441.py:451,494,867`. `close_all_runner.py:51-58` invokes them.
6. **Standalone scripts perform unguarded PositionClose / OrderCancel.** `close_all.py:64-65,124-125`; `close_all_v1.py:98-99,158-159`; `scheduled_runner_jcs_claude.py:182-183,250-251,366-367` (its own docstring at `:304` states it *"Bypasses any strategy idempotency, SL/TP guardian, or tag checks"*).

### UNKNOWN

7. **`utils/trading_core.py` vs `utils/trading_core_v2.py` aliasing** — determines whether legacy ungated paths and v3 gated paths share one implementation. Not verified.
8. **Runtime `is_bot_owned_trade` result on a genuine post-fill broker record** — the static gate is sound, but scenario 3/8 assertions about live broker tag echo were not executed (read-only, no network).
9. **Entry path `OrderCreate` is not behind any `_gated_*` wrapper** — `utils/trading_core_v2.py:274`. In-scope or out-of-scope for the invariant is not defined by the code or config.
10. **`_find_open_trade_by_id` masks fetch failures as "already closed".** `scheduled_runner_v3.py:153-154, 182-186` — fail-closed, but the state is not distinguishable from a real closure.

### Ungated mutations

- `utils/trading_core_v2.py:274` — OrderCreate (entry; no ownership gate).
- `utils/trading_core_v2.py:325-332` — TradeCRCDO / SL-TP attach on unverified post-fill trade id.
- `utils/trading_core_v2.py:334-336` — same, `trade_id` falsey branch → `attach_sl_tp_to_open_trade`.
- `utils/trading_core_v2.py:192` — TradeCRCDO (inside `attach_sl_tp_to_trade_id`, no internal ownership check).
- `utils/trading_core_v2.py:436` — TradeClose (inside `close_trade_by_id`, no internal ownership check).
- `utils/trading_core_v2.py:472` — TradeCRCDO (inside `update_trade_sl_only`, no internal ownership check).

### Position-level mutations

- `utils/trading_core_v2.py:408-413` — `PositionClose` in `close_position`; exposed via `close_pair_position` (`:610-617`).
- `utils/trading_core_v2.py:143` — `TradeCRCDO` against `trade_ids[0]` (position-derived "first trade").
- `scheduled_runner_v144.py:426,478`; `scheduled_runner_v145.py:201,253`; `scheduled_runner_v1441.py:451,494`.
- `close_all.py:124`; `close_all_v1.py:158`; `scheduled_runner_jcs_claude.py:182,250,366`.

### Instrument-as-authorization paths

- `utils/trading_core_v2.py:69-77, 114-125` — `get_open_position` → `trade_ids[0]`.
- `scheduled_runner_v144.py:458-481`; `scheduled_runner_v145.py:233-254`; `scheduled_runner_v1441.py:490-494`.

> The v3 runner is **clean** here: instrument is used only for discovery (`scheduled_runner_v3.py:140-143, 288-289`).

### Cached-`trade_id`-trusted paths

**None found in the v3 path.** The risk engine passes a `trade_id` (`risk_engine_v22.py:334, 250`), but the adapter re-reads live broker state and re-validates before mutating (`scheduled_runner_v3.py:180, 208, 245`).

> **Caveat:** the revalidation lives **only** in `scheduled_runner_v3.py`. `risk_engine_v22.py` performs no ownership check of its own, so any alternative adapter would reintroduce the trust. The current adapter is closed under `_gated_*`; that closure is a property of the adapter, not of the risk engine.

### Unproven tag propagation

- The tautological assertion `tests/test_trade_client_extensions.py:145-146` (and `:149-159`) compares mocked payload fields to the strings the test itself supplies — it does **not** prove the broker echoes the tag onto the Trade.
- `utils/oanda_execution.py:151,212` set only `clientExtensions`, never `tradeClientExtensions` — trades opened through that adapter would not be tag-managed. Not reachable from v3, but a real propagation hole in the repo.

### Scenarios not proven PASS

| Scenario | Result |
|---|---|
| 1, 2, 4, 5, 6, 7 | PASS (static) |
| 3, 8 | PASS static; **UNKNOWN** against a live account |
| 5 (fetch-failure branch) | PASS static with masking caveat → **UNKNOWN #10** |
| 9 | **FAIL** |
| 9b (entry OrderCreate) | **UNKNOWN (scope)** |
| 9c (`tradeReduced`) | **FAIL** |
| C3 | **UNKNOWN / absent** |
| C4 (malformed tag) | Divergence flagged; not an ownership bypass |
| C5 | **UNKNOWN** account mode, **FAIL** on detection |
| C6 | No ownership gate on pending orders (no mutations to gate either) |

---

## Final Determination

**HARD INVARIANT NOT PROVEN**

**Cause:** ungated mutations exist on the post-fill / `tradeReduced` path (`utils/trading_core_v2.py:274, 325-332, 334-336, 192, 436, 472`), Scenario 9 **FAILS** (no post-fill ownership verification, no UNKNOWN state, no surfaced discrepancy), and tag propagation is **UNPROVEN** (no verified broker echo; `tradeClientExtensions` absent in `utils/oanda_execution.py`).

Any one of these three is **independently sufficient** to fail the invariant.

---

## Closing Note on Audit Integrity

- No files were modified, created, or deleted.
- No bot, script, or test was executed.
- No network or broker calls were made.
- Every conclusion above is from static reading of the cited lines.

The v3 runner's **own** ownership gates are correctly implemented and correctly routed. The failures are in:

1. the **un-gated `TradingCore` primitives** they delegate to,
2. the **post-fill path**, which mutates before ownership is ever established, and
3. **unexecuted propagation evidence**.

Two items could not be resolved statically and are reported as UNKNOWN rather than assumed:

- `utils/trading_core.py` ↔ `utils/trading_core_v2.py` aliasing
- live broker tag echo on `tradeClientExtensions`