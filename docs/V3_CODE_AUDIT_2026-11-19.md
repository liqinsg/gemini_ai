# v3 代码审核报告 — scheduled_runner_v3.py

日期：2026-11-19
范围：今日 v3 的 +1497 行迁移（commit `24c1338`）及新增逻辑；对照 v1441。
方法：静态代码审查（未运行、未改码）。

> 命名说明（用户 2026-11-19 确认）：`scheduled_runner_v3.py` **就是此前的 v31**（v31 被覆盖为 v3）。本报告审核对象即 crontab 实际调用的该文件；下文所有 `scheduled_runner_v3.py` 路径/行号均指向它，结论与命名标签无关。
定位原则：v1441 为过渡品不再投入；所有结构性建议落 **v3**。每条建议必须带"后手/闭环"，无闭环不写。

---

## 0. 结论速览

| 级别 | 问题 | 位置 |
|---|---|---|
| ~~高~~ **已修复** | weak-exit **曾**既无归属预过滤、又绕过 `_gated_close_trade_by_id`；**现改走门禁**；仍缺**候选快照预过滤**（执行时有实时门禁） | `scheduled_runner_v3.py:4884` |
| 中 | +2R 档与硬 TP=2R 重合 → scale-out 在常规行情下退化为"只减一次" | `config.py:409` / `config.py:179` |
| 中 | 两层 shrink（动态仓位 × MC 概率）**相乘**，最坏 0.0625，且最小手数守则不一致 | `scheduled_runner_v3.py:4593` + `:4629` |
| 中 | 出场无统一仲裁：四条出场路径按固定顺序跑，非契约化 | `run_cycle:5794-5825` |
| 低 | runner 外层 `except` 为 fail-open（保留原仓），与模块内 fail-closed 语义不一致 | `scheduled_runner_v3.py:4647` |
| 低 | v31 删除后仅剩注释引用，无 import（安全） | 见 §8 |

**总体判断：今天 v3 的迁移本身是忠实移植，未发现重复平仓或配置漂移；但把 v1441 的"多规则堆叠、缺仲裁"一并带了过来。** 唯一需要尽快处理的 weak-exit **权属门禁已修复**（见下修复状态）；**当前最高优先残留**是 scale-out 缺执行前二次权属校验（§3 表）与出场缺统一仲裁（§9 P1）。

> **修复状态**：§0 第 1 行（weak-exit 权属门禁）**已修复** —— weak-exit 改走 `_gated_close_trade_by_id`（`scheduled_runner_v3.py:4884`），`py_compile` 通过。其余条目待处理。
> **版本边界**：本报告结论对应迁移 commit `24c1338`；文中行号以该 commit 为准，后续改动会漂移（例如 weak-exit 行号已由 `:4882` 变为 `:4884`）。

---

## 1. 今天 v3 加了什么（迁移清单）

新增函数（`git diff b05c828 873dabb -- scheduled_runner_v3.py`）：

- `_safe_emit`、`_jpy_gate_shadow_log`
- `_compute_jpy_dynamic_weights` / `_compute_jpy_dynamic_weights_v2`（JPY 动态权重）
- `_compute_extension_lot_scale`（追势减仓，新增仓位层）
- `_scale_out_scan_v31`、`_weak_exit_scan_v31`（两条出场路径）

`run_cycle` 接线：见 §2。配置来自 `config.py`（v3 `import config as _config`）与 `config_bot`。

---

## 2. 执行顺序与重复平仓

`run_cycle` 中的管理顺序（`scheduled_runner_v3.py:5794-5825`）：

```
_sltp_guardian(dry_run)                     # 1. 修 SL/TP
  ↓
per-group _maintain_group_positions(...)    # 2. 权属过滤 → 风险引擎(H1/D) → 提前退出(MA反向)
  ↓
_weak_exit_scan_v31(...)                    # 3. 全平：组内支撑分垫底
  ↓
_scale_out_scan_v31(...)                    # 4. 部分平：+1R/+2R 分批
  ↓
（其后才进入入场信号生成）
```

结论：

- **全平(3)在部分平(4)之前** —— 这是"全平优先于减仓"的正确方向，但属**巧合顺序**，非显式设计。
- 四条路径各自取**独立的新快照**（weak 用 `_all_open_trades_snapshot()`，scale-out 用 `get_all_open_trades()`），先跑的平掉后，后跑的快照里已无该单 → **同周期不会重复平仓**。
- 幂等只有风险引擎有 `pending_exits`（`_maintain_group_positions:4966` `prune_pending_exits`）；weak/scale-out **不登记** pending，仅靠下周期 prune 兜底。
- **判定：当前无实际重复平仓路径**；缺的是"谁先谁后、谁能覆盖谁"的契约（Codex 的 Exit Arbitration 观点成立，属 v3 设计输入）。

---

## 3. 权属（ownership）一致性 —— 本次最重要发现

代码库**早已有**完整归属机制：`BOT_OWNED_TAG_ROOT="GEMINIAIBOT_V3"`（`utils/oanda_state.py:18`）、`is_bot_owned_trade()`（`utils/utils.py:34`）、以及**权属门禁封装** `_gated_close_trade_by_id()`（`scheduled_runner_v3.py:513`，非本 bot 单直接 REFUSE）。

三条管理路径的实际用法：

| 路径 | 权属预过滤 | 关闭调用 | 安全 |
|---|---|---|---|
| `_maintain_group_positions` | `is_bot_owned_trade` + `is_strategy_trade` | `_gated_close_trade_by_id` | ✔ |
| `_scale_out_scan_v31` | `is_bot_owned_trade`（`:4730`） | `close_trade_partial`（**仅快照预过滤，无执行前实时校验**） | ⚠ 有预过滤 / 缺二次校验 |
| `_weak_exit_scan_v31` | **无**（用户决定，`:4819`） | `_gated_close_trade_by_id`（`:4884`，**已修复**） | ✔（执行时门禁） |

~~**weak-exit 是唯一缺口。**~~ **已修复**：现走门禁（bot-only 账户行为不变）；仍缺**候选快照预过滤**，执行时由门禁实时把关（与 Doc B §1.4/§3.3 的 TOCTOU 修法一致）。

**已实施的加固**：`:4882` 的直接 `_trading_core.close_trade_by_id(...)` 已换为 `_gated_close_trade_by_id(tid, client_request_id=..., gate_name="WEAK-EXIT")`（`:4884`）。bot-only 账户下门禁永不拒绝 → 行为不变；一旦 account 被其它进程/人工介入，自动拒绝而非误平。**注意**：其 `client_request_id` 仍为 `WEAKEXIT_{tid}_{_stamp}`（带时间戳，跨周期不幂等），与 Doc B §4.1 要求的稳定 `EXIT_WEAK_{trade_id}` 仍有差距。

> 这同时**反驳 Dola 的建议**："系统无归属机制、建议自造 `client_request_id` 过滤"——机制已存在，`client_request_id` 也不是归属标识（归属在 `clientExtensions.tag`）。

---

## 4. TP vs SCALE-OUT —— v3 的真实形态（与 v1441 不同）

- **v3 没有 v1441 的 MC TP 压缩机制**（无 `tp_mult` / `TP_MULT`）。v3 的 MC 只做 `_widen_sl`（`scheduled_runner_v3.py:2711`），把 SL 与 TP **按同一比例缩放**、RR 不变。
  → 因此 **Dola 的"把 TP 压到 0.8R 的一行修复"对 v3 完全不适用**（那是 v1441 特有缺陷）。
- 硬 TP = `JPY_ATR_RR_MULTIPLE × SL = 2.0R`（`config.py:179`）；分批档 = `[(1.0,0.5),(2.0,0.5)]`（`config.py:409`）。
- **精确结论**：
  - 常规行情（MC 未加宽，f=1）：硬 TP 恰好落在 **+2R** → 第二档被 broker 硬 TP 取代 → scale-out 实际退化为"**+1R 减半，其余跑到 2R 硬 TP**"，与文档"分两档管理"不符。
  - CONSOLIDATION（f>1，SL/TP 等比外推）：硬 TP 被推到 **>2R** → 此时 +2R 档才能在硬 TP 前触发。**冲突只在非加宽行情出现。**
- 且剩余 50% **无保本/追踪**（SL 仍为初始 SL）→ 拿到的 +1R 可能被后半仓回吐，本质不是 "managed exit"。

**判定：不是 bug，是设计未闭合。** 需按 Codex 的二选一收敛：
- **Fixed TP**：硬 TP 正常用，关闭 SCALE_OUT；
- **Managed Exit**：硬 TP 外推（留出后面档位空间），由 scale-out + 剩余仓位保护负责最终退出，且剩余仓必须补保本/追踪。

---

## 5. fail 语义

- 模块内（`utils/prob_zone_sizing.py` docstring）：数据缺失/退化 → `size_scale = F_LO(0.25)`，且 `FAILCLOSED_SKIP=False` → **仍开仓（小仓）**。
  → 这是 **Fail-Degraded**，不是真 Fail-Closed（真 fail-closed 应"不确定就不开"）。**Codex 的术语纠正正确。**
- runner 外层 `except`（`scheduled_runner_v3.py:4647`）→ 保留 `_order_units`（**满仓**）= **fail-open**，与模块"绝不回退到 1.0"的契约相反。
- 但 `compute_prob_plan` **永不抛异常** → 该外层 except 近乎**死代码**。故风险低，但修复成本仅为一行，建议对齐：外层 except → `units = max(floor, int(base×F_LO))`。
- "真正的 fail-closed" 开关**已存在**：`FAILCLOSED_SKIP=True`（`config.py:395`）会让数据异常时 skip 不建仓。**当前为 OFF，是策略选择**，不是缺陷——但这是你唯一能一键切换 fail 姿态的杠杆，值得显式决策。

---

## 6. 仓位：v3 有两层 shrink 叠加（新发现）

| 层 | 函数 | 行为 | fail 姿态 |
|---|---|---|---|
| 动态仓位（追势减仓） | `_compute_extension_lot_scale`（`:3208`） | \|ext\|<1ATR→1.0；[1,2)→0.5；≥2→0.25 | **fail-open**（→1.0） |
| MC 概率 | `_compute_prob_plan`（`:4629`） | conv/risk_cap/regime 取 min，floor 0.25 | fail-degraded（→0.25） |

- 应用顺序：动态仓位先（`:4593`）→ 概率后（`:4629`），**最终 units = base × dlot × prob**，即**相乘**。最坏 `0.25 × 0.25 = 0.0625`。
- 最小手数守则不一致：dlot 有"<1 手则回退满仓"的守则（`:4599-4612`），概率层按 `MIN_LOT_LIVE=1` 各自处理 → live `LOT_SIZE=1` 时两层边界行为需实测。
- **关键：两层都不是风险金额化定仓** —— 都基于固定 `_EFFECTIVE_LOTS` 缩放，与 SL 距离、账户净值无关。**结构性缺口仍在**（与两篇评审共识一致）。

---

## 7. SHADOW 语义（v3 与 v1441 一致，仍是陷阱）

- v3 三处同为 **`ENABLED`=是否执行，`SHADOW`=执行的同时再写影子日志**（`config.py:376/403/419`；`scheduled_runner_v3.py:4711/4822` docstring 同）。
- v3 `--dry-run` 默认 **False**、`--profile` 默认 **2**（`:194/:198`）→ 生产环境**会尝试真实下单**。**注意区分**：是否连 **live 账户**仅由 `--live` 决定（`:281` 才设 `OANDA_ENV=live`；否则 `get_oanda_profile("practice")`，`:284`）。故"`dry-run` 关闭"≠"连 live 账户"。
- 代码库另处（`POST_EXIT_GATE`）才是真正的 shadow/live 二分 → **命名不统一**，正是误导两篇外部评审的根源。
- 建议：把 `SHADOW` 改名（如 `SHADOW_LOG`）或统一为 `MODE: live|shadow`，消除歧义。

---

## 8. v31 残留引用

v31 内容已并入 v3（旧 `scheduled_runner_v31.py` 删除/覆盖后），全库对 v31 的引用仅剩**注释/docstring**（`scripts/analyze_jpy_gate.py`、`tests/…`、manual_checks 等），**无任何 import 或调用** → **改名安全，无需回滚**。

---

## 9. 改进方向（每条带闭环）

> 格式 = 观察 → 判定阈值 → 达标/不达标动作。

**P0 — 安全与风险基础**
- **P0-a weak-exit 权属门禁（已修复）**：`:4882` 已改用 `_gated_close_trade_by_id`（现 `:4884`）。**残留动作**：补**候选快照预过滤**（执行前实时门禁已在位）。
  闭环：上线后统计 `[WEAK-EXIT] OWNERSHIP MISMATCH` 次数；>0 说明 account 被外部占用，立即复核账户隔离。
- **P0-a′ scale-out 执行前二次权属门禁（当前最高优先残留）**：`:4779` 的 `close_trade_partial` 仅有**快照期** `is_bot_owned_trade` 过滤（`:4730`），**平仓时无实时校验**（TOCTOU）。
  闭环：纳入 Doc B §3.3 统一执行出口，每次实际部分平仓都做实时权属校验；统计 `OWNERSHIP_CHANGED` 次数。
- **P0-b 风险金额化定仓**：`units = Equity×Risk% / (SL距离 × 报价→账户货币汇率)`，用**最终确认的 SL**反推（信号→SL→风险→units，而非先定手数）。
  闭环：每笔记录 `risk_amount`；**逐笔对照**其自身目标 `Equity×Risk%×D_final`，相对偏差 `|actual−target|/target < 5%` 视为达标（**不再要求不同 pair 的 `risk_amount` 彼此相等/同方差**——`D_final` 本就使各笔预算不同，见 Doc A §8）。
- **P0-c fail 语义对齐**：外层 except → floor 而非保留原仓；并**显式决策** `FAILCLOSED_SKIP` 开关姿态。
  闭环：模拟异常注入测试必须得到"缩仓/拒单"，不得为满仓。

**P1 — 退出仲裁**
- 把固定顺序升级为显式契约（L0 硬保护 → L1 全平 → L2 部分 → L3 剩余仓位管理），写明"全平覆盖部分、交易不存在即停管、权属不明即拒"。
- TP 二选一（Fixed TP / Managed Exit）；Managed Exit 必须补剩余仓位保本/追踪。
  闭环：每次退出记录 `exit_reason` + 最终 R；月度按 `exit_reason` 统计净 R，若某规则长期为负 → 关该规则。

**P2 — SL/TP 算法（实验，带后手）**
- ATR / 结构 S/R / Hybrid 三方案**并行影子**，各记录 SL 距离、TP 距离、初始 R、MAE、MFE、SL/TP 触发率。
  闭环：设定判定阈值（如 Hybrid 的"成本后净 R"需高于 ATR ≥ X% 且样本 ≥ N 笔）→ 达标签切换、不达标保持 ATR，**不留悬空观察**。

**P3 — per-pair 差异化**
- 先出 per-pair 绩效表（净 R、胜率、SL/TP 触发比、平均持仓、跳空/滑点），再决定是否差异化，避免参数优化。
  闭环：任一对连续 M 周净 R 为负且显著 → 单独调参或下线该对。

---

## 10. 与两篇外部评审的净裁定

**采纳**：Codex 的 Exit Arbitration / 定仓顺序（信号→SL→风险→units）/ "0.25 是 Fail-Degraded" / "别把未校准 ML 放到 SL/TP 核心"。

**降级**：所有权过滤紧迫性——v1441(profile1→account-001) 与 v3(默认 profile2→account-002) **不同账户**，两者不会互平；weak-exit 门禁**已于 P0-a 挂上**，残留为 **scale-out 执行前二次门禁（P0-a′）** + 确认 account-001 无第三方占用。

**剔除（对 v3 不成立）**：
- Dola 的整套"改 v1441 五处"补丁（v1441 将废弃）；
- Dola 的"Shadow 跑 24h 再关"流程（**须区分两件事**：`--dry-run` 默认关闭 → runner **会尝试真实下单**；但**是否连 live 账户**仅由 `--live` 决定，未传时 `OANDA_ENV` 仍为 practice（`scheduled_runner_v3.py:281-284`）。故“当前已是 LIVE”不成立，正确表述是“会真实下单、默认连 practice”）；
- Dola 的 `client_request_id` 归属过滤与 `build_strength_matrix().get(pair)` 一行修（均与真实接口/机制不符）；
- Dola 的 `ATR_RR_MULTIPLE=2.6`（v3 无 TP 压缩机制，且直接改值会变更利润分布；应按 Fixed/Managed 二选一）。

---

## 附：需人工确认的事实

1. `run.env` 未设 `EXPECT_ACCOUNT_ID`/`OANDA_ENV`（已确认当前 run.env）→ 账户安全网未启用；v3 若 `--live` 会打印 "no account guard active" 警告。
2. 生产机 crontab 不在本环境（本机 `NO_CRONTAB`）→ 请在部署机确认 v1441/v3 的账户与时段边界。
3. live 下 `LOT_SIZE=1` 时，动态仓位 × 概率两层 shrink 的实际最小手数行为需跑一次 dry-run 验证。
