# Base-Currency Strength Strategy v3 — 命令行使用指南

**入口**: `scheduled_runner_v3_op.py`  
**环境**: Python 3.10+ / OANDA API  
**配置优先级**: CLI 参数 > `run.env` > `config.py` 默认值  
**账户隔离**: 每个 profile 独立账户 + 独立文件锁，多账号可并行

---

## 目录
- [基本调用格式](#基本调用格式)
- [分组说明](#分组说明)
- [参数速查表](#参数速查表)
- [参数详解](#参数详解)
- [运行模式对照](#运行模式对照)
- [常用组合示例](#常用组合示例)
- [配置文件关系](#配置文件关系)
- [故障排查速查](#故障排查速查)

---

## 基本调用格式
```bash
# 通用形式（使用 conda/miniconda 环境，避免系统 Python 混淆）
conda run -n <env> python scheduled_runner_v3_op.py [OPTIONS]

# 或显式路径
$HOME/miniconda3/envs/<env>/bin/python scheduled_runner_v3_op.py [OPTIONS]
```

---

## 分组说明

策略按 quote 货币分组，每个 group 独立跑 MAINTAIN（风控 + 早期退出）和 STRATEGY（信号生成）。定义在 `config_bot_v3.STRATEGY_GROUPS`：

| Group Key | quote_ccy | tag_prefix | Instruments（自动从 STRENGTH_PAIRS 过滤 `_<quote>` 后缀） | 特殊参数 |
|-----------|-----------|------------|---------------------------------------------------------|---------|
| **JPY** | JPY | `JPY-STRENGTH` | `USD_JPY`, `EUR_JPY`, `GBP_JPY`, `AUD_JPY`, `CAD_JPY`, `NZD_JPY`, `CHF_JPY` | EXTREMES-ONLY（abs(score)≥1.8 升 OVERRIDE）+ Layer 1 gate（`JPY_REQUIRE_GLOBAL_EXTREME`，默认 true）+ Layer 2 方向 gate（`JPY_REQUIRE_EXTREME_RANK`，默认 false）+ 排名裁剪（`JPY_GATE_EXCLUDE_CURRENCIES`，两 gate 共用） |
| **USD** | USD | `USD-STRENGTH` | `EUR_USD`, `GBP_USD`, `AUD_USD`, `NZD_USD`, `USD_CHF`, `USD_JPY`, `USD_CAD`, `USD_SGD` | — |
| **CHF** | CHF | `CHF-STRENGTH` | `USD_CHF`, `EUR_CHF`, `GBP_CHF`, `AUD_CHF`（当前 STRENGTH_PAIRS 里只有 `USD_CHF`） | `MIN_STRENGTH_PASSING_PAIRS=1`, `MIN_DOMINANT_PAIRS=1` |

> 📌 **注意**: Instruments 不是手工写死在 config 里的，而是 `_run_single_group()` 里通过 `[p for p in STRENGTH_PAIRS if p.endswith("_" + quote_ccy)]` 动态过滤的。所以往 STRENGTH_PAIRS 里加 `EUR_CHF` 之类的对，CHF 组会自动覆盖到。

### 控制分组的三种方式

| 方式 | 效果 | 优先级 |
|------|------|--------|
| `--no-trade-jpy` / `--no-trade-chf` | 禁用某个组，其他照常 | CLI > run.env `TRADE_<CCY>` > 默认 true |
| `--trade-jpy-only` / `--trade-chf-only` | 只跑一个组，其余全部跳过 | CLI-only 模式**最高优先**（与 `--no-trade-*` 互斥） |
| `run.env TRADE_JPY=false` / `TRADE_CHF=false` | 禁用某个组 | 仅当 CLI 没显式覆盖时生效 |

---

## 参数速查表

| 参数 | 短写 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `--profile` | `-p`, `--account`, `-a` | int | `2` | OANDA 账户 profile 编号（1, 2, ...） |
| `--live` | — | flag | 未指定 = DEMO | 切换到实盘（LIVE）环境 |
| `--dry-run` | — | flag | `run.env DRY_RUN` | 分析信号但不下单 |
| `--lots` | — | int | `run.env LOT_SIZE` | 单笔手数/单位，CLI 最高优先级 |
| `--max-entries` | `-n` | int | `1` | 单轮 basket 最大入场数；`1`=只做 top 信号 |
| `--trade-jpy` | — | `true\|false` | `true` | 启用/禁用 JPY 组执行 |
| `--no-trade-jpy` | — | flag | — | `--trade-jpy false` 的快捷写法 |
| `--trade-jpy-only` | — | flag | — | 只跑 JPY 组，其他组全部跳过 |
| `--no-trade-chf` | — | flag | — | 禁用 CHF 组执行 |
| `--trade-chf-only` | — | flag | — | 只跑 CHF 组，其他组全部跳过 |
| `--use-macd` | — | `true\|false` | `run.env USE_MACD` | 显式覆盖 MACD 过滤器开关 |
| `--no-use-macd`<br>`--no-macd` | — | flag | — | `--use-macd false` 的快捷写法 |
| `--debug` | — | 1\|2\|3 | 未使用 | 预留，当前脚本未消费此值 |
| `--help` | `-h` | — | — | 显示 argparse 帮助并退出 |

> 💡 **注意**: `--live` 仅切换账户环境，不影响 `--dry-run`、`--lots` 等行为。实盘发单必须 `--live` + 不带 `--dry-run`。
>
> ⚠️ **互斥**: `--trade-jpy-only` 和 `--trade-chf-only` 不能同时传，argparse 会直接报错退出。

---

## 参数详解

### `--profile / -p / -a`
OANDA 多账户隔离。脚本会查找 `OANDA_ACCOUNT_ID_<profile>` 环境变量，配合文件锁实现同机多账号并行无冲突。
```bash
# profile=1 → 查找 OANDA_ACCOUNT_ID_1 或 OANDA_ACCOUNT_ID_1_LIVE
python scheduled_runner_v3_op.py -p 1 --live
```

### `--live`
布尔开关。设置 `OANDA_ENV=live`，让 `config_oanda.py` 加载实盘凭证、实盘 lot size。**不带 `--live` 时一律走 DEMO**，即使账户里有实盘凭证也不会发真实单。

### `--dry-run`
布尔开关。直接禁止 `execute_market_trade()` 下到 broker，所有信号正常生成、评分、打印，最终标记 `DRY-RUN → skipping order submission` 退出。
- 和 `--live` 可以同时出现：实盘凭证 + 模拟下单 = 最安全的预演
- 优先级高于 `run.env DRY_RUN`

### `--lots N`
硬覆盖单笔手数。如果不传，脚本从 `run.env LIVE_LOT_SIZE` 或 `DEMO_LOT_SIZE` 读。
```bash
python scheduled_runner_v3_op.py --live --lots 1000
```

### `--max-entries N / -n N`
单轮 basket 里最多同时开仓数。
- `-n 1`（默认）：只取最强信号，Gate 全部通过才执行
- `-n 2+`：做 basket，脚本会先算完所有组信号、排序列出 top-N、再按顺序执行（每个 entry 独立走 position cap / override 上限）
- 值必须 ≥ 1，argparse 会拒绝 `0`

### `--trade-jpy true|false`
显式启用或禁用 JPY 组（JPY_BASE / JPY_QUOTE 两个分组的 MAINTAIN + STRATEGY 全停）。不传则从 `run.env TRADE_JPY` 读，默认 `true`。
```bash
python scheduled_runner_v3_op.py --trade-jpy false    # 本 cycle 完全跳过 JPY
python scheduled_runner_v3_op.py --trade-jpy true     # 显式启用
python scheduled_runner_v3_op.py --no-trade-jpy       # 等同于 false
```

### `--use-macd true|false`
覆盖 MACD 过滤器。不传从 `run.env USE_MACD` 读。
```bash
python scheduled_runner_v3_op.py --use-macd false    # 所有策略信号跳过 MACD gate
python scheduled_runner_v3_op.py --no-use-macd       # 同上
```

### `--no-trade-chf`
禁用 CHF 组。CLI 优先；不传则看 `run.env TRADE_CHF`；都没配默认启用。
```bash
python scheduled_runner_v3_op.py --no-trade-chf --live
```

### `--trade-jpy-only` / `--trade-chf-only`
**Group-Only 模式**：整轮 cycle 只跑指定分组，其余全部跳过（包括 MAINTAIN 和 STRATEGY）。典型场景是做单组调试或 A/B 对比。
- 与 `--no-trade-*` 互斥：`--trade-jpy-only` 暗含 "除 JPY 外全禁"
- 两个一起传会直接报错退出
```bash
python scheduled_runner_v3_op.py --trade-jpy-only --live      # 只做 JPY 组
python scheduled_runner_v3_op.py --trade-chf-only --dry-run  # 只分析 CHF 组信号
```

---

## 运行模式对照

| 场景 | 命令 | 发单？ | 配置来源 |
|---|---|---|---|
| 🔬 默认 DEMO 预览 | `python scheduled_runner_v3_op.py` | ❌ | run.env DEMO_* |
| 🧪 DEMO + 指定手数 | `python scheduled_runner_v3_op.py --lots 5000` | ❌ | DEMO_LOT_SIZE=5000 |
| 🧪 DEMO + 禁 JPY | `python scheduled_runner_v3_op.py --trade-jpy false` | ❌ | JPY 组跳过 |
| 🧪 DEMO + 禁 CHF | `python scheduled_runner_v3_op.py --no-trade-chf` | ❌ | CHF 组跳过 |
| 🧪 DEMO + 只跑 JPY | `python scheduled_runner_v3_op.py --trade-jpy-only` | ❌ | 仅 JPY 组 |
| 🧪 DEMO + 只跑 CHF | `python scheduled_runner_v3_op.py --trade-chf-only` | ❌ | 仅 CHF 组 |
| ⚡ 实盘标准 | `python scheduled_runner_v3_op.py --live` | ✅ | run.env LIVE_* |
| ⚡ 实盘 + 覆盖手数 | `python scheduled_runner_v3_op.py --live --lots 1` | ✅ | CLI 最高优先 |
| ⚡ 实盘 + basket | `python scheduled_runner_v3_op.py --live -n 2` | ✅ | 最多入场 2 个信号 |
| ⚡ 实盘 + 禁 MACD | `python scheduled_runner_v3_op.py --live --use-macd false` | ✅ | 跳过 MACD gate |
| ⚡ 实盘 + 禁多组 | `python scheduled_runner_v3_op.py --live --no-trade-jpy --no-trade-chf` | ✅ | 只剩 USD 组 |
| 🛡️ 实盘干跑（最安全预演） | `python scheduled_runner_v3_op.py --live --dry-run` | ❌ | 实盘逻辑但不下单 |
| 🔁 多 profile 并行 | `python scheduled_runner_v3_op.py -p 1 --live` + `... -p 2 --live` | ✅ | 独立锁、独立账户 |

---

## 常用组合示例

### 1) 首次跑 profile=1 DEMO（推荐默认）
```bash
python scheduled_runner_v3_op.py -p 1
```

### 2) 实盘 profile=1，标准 1 手
```bash
python scheduled_runner_v3_op.py -p 1 --live --lots 1
```

### 3) 实盘 profile=2，basket 模式
```bash
python scheduled_runner_v3_op.py -p 2 --live -n 3
```

### 4) 实盘干跑——验证配置 + 信号，绝不发单
```bash
python scheduled_runner_v3_op.py -p 1 --live --dry-run
```

### 5) 禁 JPY 组（常见于 JPY 流动性差或有独立策略时）
```bash
python scheduled_runner_v3_op.py --trade-jpy false --live
```

### 6) 禁 MACD 过滤器，放宽入场条件
```bash
python scheduled_runner_v3_op.py --use-macd false --live
```

### 7) 定时调度（cron，每 15 分钟跑一次 profile=1）
```bash
*/15 * * * * $HOME/miniconda3/envs/ai-sprint/bin/python \
  /home/qili/projects/gemini_ai/scheduled_runner_v3_op.py \
  -p 1 --live --lots 1 >> /home/qili/projects/gemini_ai/bot.log 2>&1
```

### 8) 双 profile 并行（不同账户互不干扰）
```bash
# crontab 里各开一条
*/15 * * * * ... python scheduled_runner_v3_op.py -p 1 --live
*/15 * * * * ... python scheduled_runner_v3_op.py -p 2 --live
```

---

## 配置文件关系

```
run.env 优先级（自高而低）
├── CLI 参数           ← --lots / --trade-jpy / --use-macd / --live / --dry-run
├── run.env 文件值      ← LIVE_LOT_SIZE / DEMO_LOT_SIZE / TRADE_JPY / USE_MACD / DRY_RUN
└── config.py 内置默认值 ← CURRENCIES / STRENGTH_PAIRS / STRENGTH_TIMEFRAMES 等
```

关键 run.env 键 → CLI 对应：

| run.env | CLI 覆盖 | 说明 |
|---|---|---|
| `LIVE_LOT_SIZE` / `DEMO_LOT_SIZE` | `--lots` | CLI 直接覆盖 |
| `DRY_RUN` | `--dry-run` | CLI 覆盖 |
| `USE_MACD` | `--use-macd` / `--no-use-macd` | CLI 显式覆盖 |
| `TRADE_JPY` | `--trade-jpy` / `--no-trade-jpy` | CLI 显式覆盖 |
| `JPY_REQUIRE_GLOBAL_EXTREME` | — | JPY Layer 1 gate（runner 级，默认 true）；JPY 必须 TOP 或 BOTTOM 排名才放行 |
| `JPY_REQUIRE_EXTREME_RANK` | — | JPY Layer 2 gate（策略内部，方向感知，默认 false）；过了 Layer 1 后还要过这个方向过滤 |
| `JPY_GATE_EXCLUDE_CURRENCIES` | — | JPY gate 排名裁剪（Layer 1 + 2 共用）；逗号分隔币种代码，排除从 gate 排名中去掉 |
| `OANDA_ACCOUNT_ID_<profile>[_LIVE]` | — | profile 查找，无 CLI 覆盖 |

---

## 故障排查速查

| 现象 | 最可能原因 | 解决 |
|---|---|---|
| 程序跑了但不下单 | 没带 `--live`（走 DEMO）或带了 `--dry-run` | 实盘发单必须 `--live` 且无 `--dry-run` |
| 账户 ID 为空 / profile 找不到 | `run.env` 里没配 `OANDA_ACCOUNT_ID_<profile>` | 检查 `OANDA_ACCOUNT_ID_1` / `_LIVE` 是否存在 |
| 两个 runner 互相等待 | 同一个 profile 被并行启动（锁等待） | 换不同 `-p`，或停掉重复进程 |
| JPY 组全部 SKIP | `run.env TRADE_JPY=false` 或 CLI 传了 `--trade-jpy false` | 确认 banner 行 `[SKIP GROUP JPY] TRADE_JPY disabled` |
| `--max-entries 0` 报错 | argparse 拒绝 | 必须 ≥ 1 |
| 脚本启动后静默退出 | `fetch_oanda_candles` / API 网络错误 | 检查 OANDA 凭证、网络、VPN |
| run.env 改了没生效 | CLI 参数优先覆盖，或缓存了旧进程 | 确认命令行没带覆盖参数；完全重跑 |
| 多 profile 共享同一订单 | `config_oanda.py` 里 profile 映射写错 | 每个 profile 必须对应独立 account ID |

---

## 架构参考（v3 vs 旧版本）

旧版 `scheduled_runner_v144.py` 单账户单因子；v3 改为：
- Multi-Group：JPY_BASE / JPY_QUOTE / USD 等组独立信号生成，互不阻塞
- Global Strength Matrix：`build_strength_matrix()` 一次算全局货币强度，所有组共享
- Basket 入场：`--max-entries N` 一次开 N 个，Net Exposure Cap 跨组限制
- Override 通道：信号可通过 override 绕过 position cap
- 盘后缓存：`_market_closed` 开关启用 cycle-scope snapshot + candle memoization

> 📁 **原文件** `scheduled_runner_v3.py` 保持不变；**修复/新功能** 都在 `scheduled_runner_v3_op.py`。