# JPY Strength Strategy — Cheatsheet

**Runner**: `scheduled_runner` · **Version**: `1.4.4.1` · **Broker**: OANDA (practice 默认, `--live` 切实盘)
`--lots <N>` 手数覆盖参数

## 1. 运行命令速查

```bash
# 默认 profile2 / practice / 只读扫描
python scheduled_runner_v2.py

# 常用组合
python scheduled_runner_v2.py -p 3 --dry-run              # profile3 模拟盘只读扫描
python scheduled_runner_v2.py -p 4 --live --lots 2000     # profile4 实盘, 手数覆盖2000
```

## 2. CLI 参数

| 参数 | 简写 | 默认 | 说明 |
|---|---|---|---|
| `--profile` | `-p / -a / --account` | `2` | OANDA/策略 profile 号 (代码内仅定义 2/3/4) |
| `--live` | — | off | 设 `OANDA_ENV=live`，否则 practice |
| `--debug` | — | None(STRICT) | `3`=full open · `2`=medium · `1`=mild |
| `--dry-run` | — | off | 只扫描/读仓，不开仓/平仓/改单 |
| `--lots` | — | None | 覆盖下单单位(units)，整数 |

## 3. Profile 风险对比（高 → 低）

| 维度 | **Profile4 ⚠️最高** | Profile2 中 | **Profile3 ✅最低** |
|---|---|---|---|
| `MIN_CONVICTION_SCORE` | 15.0（最松） | 30.0（最严） | 20.0 |
| `MIN_SCORE_GAP` | 0.05 | 0.10 | 0.10 |
| `MAX_OPEN_POSITIONS` | **10** | 3 | 6 |
| `MAX_OPEN_PER_RUN` | **3** | 1 | 2 |
| 趋势过滤 `TREND_FILTER` | 开 | **关** | 开 |
| 周线 `WEEK_EMA100_FILTER` | **关** | 关 | **开（最严）** |
| XGB / MC 阈值 | 0.52 / 52% | 0.52 / 52% | **0.55 / 55%** |
| ATR 最小波动过滤 | 开 | 关 | **开** |
| `SL_MAX_ALLOWED_PIPS_JPY` | **500（极宽）** | 默认窄 | 默认窄 |
| `MAX_HOLD_BARS` (15m) | 12 (~3h) | **24 (~6h 最久)** | 12 (~3h) |
| 保本 `BE_TRIGGER_ATR_MULT` | 1.5（早） | **2.5（最晚）** | 1.5（早） |
| Trailing `TRAIL_ATR_MULT` | 1.5（紧） | **2.8（最宽）** | 1.5（紧） |
| Demo 默认手数 | 5,000（减半） | 10,000 | 10,000 |
| 其他 | `USE_H4_ESCALE`+`TP_LINK_SL` | — | `SL_ZONE_TRAILING`+D1分组 |

> ⚠️ Live 下手数优先级见 §7，P4 的 5000 仅在 practice 且无 env 覆盖时生效。

## 4. 幂等开仓（Tag + Comment）

- **Tag 格式**：`JPY-STRENGTH_{PAIR}_{SIDE}_{YYYYMMDD}`（日期取 UTC）
  - 例：`JPY-STRENGTH_AUD_JPY_SELL_20260923`
- **Comment 格式**：`v1.4.4.1|entry=xx.xxxxx|SL=xx.xxxxx|TP=xx.xxxxx`
- **识别**：`tag.startswith("JPY-STRENGTH")` → 策略单；否则视为手动/历史单，不干扰。
- **Pair 级阻断**（`_check_pair_level_strategy_position`）：
  - 同方向已有策略单 → `same-direction duplicate`，BLOCK，跳过
  - 反方向已有策略单 → `opposite-direction`，BLOCK + **平掉原有反向仓**（`CLOSE_ONLY`），本周期不再开新仓，并清紧急锁
  - 有同 pair 策略挂单（pending）→ BLOCK
  - 查仓接口异常 → **fail closed**（视为不可开）

## 5. SL/TP Guardian（每周期必跑）

| 常量 | 值 | 含义 |
|---|---|---|
| `PRICE_PRECISION_TOL` | 0.001 | 偏差 < 此 → `NO_CHANGE` |
| `STRATEGY_UPDATE_THRESHOLD` | 0.005 | 偏差 < 此 → `MONITOR_ONLY` |
| 偏差 ≥ 0.005 | — | `UPDATE_REQUIRED` |

- 计算基准：`SL = entry ± SL_PIPS·pip`；`TP = entry ± TP_PIPS·pip·TP_RATIO`（JPY pip=0.01，否则 0.0001）
- ⚠️ **当前实现注意**：`_validate_and_repair_sltp` 中只要 OANDA 端已存在 SL/TP 订单，即标记 `DRM_MANAGED` 不再更新；**仅在缺失时补挂**。"偏差则更新(±0.2%)"逻辑在本版被短路，实际未生效。
- 补挂后二次 `get_trade_details` 确认：`CONFIRMED / NOT_CONFIRMED / PARTIAL / FAILED`。

## 6. MC Regime → 交易模式

| Regime 包含 | mode | 行为 |
|---|---|---|
| `CONSOLIDATION` | cautious | `max_pos=MC_MAX_POSITIONS_CONSOLIDATION`，需过 `MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION`，否则 HOLD |
| `STRONG` + `MOMENTUM` | aggressive | top-N 篮子执行，TP 乘数放大 |
| 其他 / `NO_MC_DATA` | normal | 中性参数 |

- 影子乘数（仅记录）：STRONG_MOMENTUM `0.90` · NEUTRAL `1.00` · CONSOLIDATION `1.10`
- 篮子执行时：JPY 交叉对候选方向不一致 → **触发 `emergency_close_all_jpy_v144()` 全平 JPY 仓**
- `MAX_NEW_ENTRIES_PER_CYCLE` 默认 1，达到即停篮子循环。

## 7. 手数解析优先级（高 → 低）

1. CLI `--lots N`
2. `run.env`：live→`LIVE_LOT_SIZE`，practice→`DEMO_LOT_SIZE`
3. `config_bot`：live→`DEFAULT_LOT_SIZE_LIVE=1000`，practice→`LIVE_LOT_SIZE=10000`（P4 覆盖为 5000）

## 8. 锁机制

| 锁 | 路径 | 行为 |
|---|---|---|
| 进程锁 | `/tmp/runner_{profile}.lock` | `flock(LOCK_EX|LOCK_NB)`，冲突即 `sys.exit(0)`（防重叠运行） |
| 紧急锁 | `<PROJECT_ROOT>/.emergency_close_lock_v144` | 阻止**新入场**，但仍扫描/处理退出信号；正常反向平仓后会自动清除 |
| 紧急全平 | `emergency_close_all_jpy_v144()` | ⚠️ 默认 `require_practice_check=True`，**仅 practice 执行**；live 直接 abort |

## 9. 退出逻辑

- **Early-Exit**：持仓 JPY 对且 `check_ma5_alignment` 方向与持仓相反 → 立即平仓。
- **PostExitGate**：`POST_EXIT_GATE_ENABLED` 控制是否启用；`POST_EXIT_GATE_SHADOW=True` 时只记录不拦截。
- **`final_action` 枚举**：`HOLD / ENTER / BLOCKED / CLOSE_ONLY / CLOSE_FAILED / BLOCKED_BY_EMERGENCY_LOCK`。

## 10. 关键目录/文件

- 影子日志：`logs/post_exit_gate_shadow.jsonl`（`POST_EXIT_SHADOW_LOG_PATH` 可覆盖）
- Cooldown：`cooldown_profile{N}.json` · 结果：`daily_results_profile{N}/`
- 启动抖动：`apply_jitter(1~5s)` 防 cron 并发撞单。
