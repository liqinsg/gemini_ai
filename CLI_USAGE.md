# FX JPY Strength Bot — 命令行使用指南
**版本**: v1.4.5  
**入口**: `scheduled_runner_v144.py`  
**环境**: Python 3.10+ / OANDA API  
**配置优先级**: CLI 参数 > `run.env` > 代码默认值

---

## 目录
- [基本调用格式](#基本调用格式)
- [核心参数详解](#核心参数详解)
- [运行模式对照](#运行模式对照)
- [常用组合示例](#常用组合示例)
- [配置文件关系](#配置文件关系)
- [返回/结果解读](#返回结果解读)
- [故障排查速查](#故障排查速查)

---

## 基本调用格式
```bash
# 通用形式
python scheduled_runner_v144.py [OPTIONS]

# 指定环境 Python 解释器（推荐，避免版本混淆）
$HOME/.venv/bin/python scheduled_runner_v144.py [OPTIONS]
```

---

## 核心参数详解

| 参数 | 短写 | 说明 | 默认值 | 生效范围 |
|---|---|---|---|---|
| `--live` | — | 实盘模式：读取完整阈值配置、发送真实订单 | 未指定=演示模式 | 全部 |
| `--dry-run` | — | 分析但不发单；覆盖 `run.env` 中 `DRY_RUN` | 由 `run.env` 决定 | 全部 |
| `--no-dry-run` | — | 允许发单；等价于 `DRY_RUN=false` | 由 `run.env` 决定 | 全部 |
| `--lots N` | `-a N` | 单次交易手数/单位；优先级最高 | `LIVE_LOT_SIZE` / `DEMO_LOT_SIZE` | 全部 |
| `--min-gap X.XXX` | — | 最小强弱缺口阈值（USD_JPY 口径） | `0.9314` | `--live` |
| `--max-entries N` | — | 单轮最多入场标的数量 | `1` | `--live` |
| `--align-required N` | — | 对齐周期数：`2`=多数通过 / `3`=全部一致 | `3` | `--live` |
| `--mc-neutral-tp X.X` | — | 中性市场止盈倍率 | `1.5` | `--live` |
| `--gap-threshold X.X` | — | "强"信号分类门槛 | `1.5` | `--live` |
| `--strict-align X.X` | — | 对齐严格度基准 | `3.0` | `--live` |
| `--help` | `-h` | 显示帮助信息并退出 | — | — |

> 💡 **注意**：不带 `--live` 时，筛选类参数（`--min-gap` / `--align-required` 等）**不生效**，保持向后兼容原有逻辑。

---

## 运行模式对照

| 场景 | 命令示例 | 发单？ | 配置来源 |
|---|---|---|---|
| 🔬 本地模拟（推荐日常） | `python scheduled_runner_v144.py` | ❌ 仅分析 | `run.env` DEMO_* + 默认筛选 |
| 🧪 指定手数模拟 | `python scheduled_runner_v144.py -a 5000` | ❌ | DEMO_LOT_SIZE=5000 |
| ⚡ 实盘 + 完整筛选 | `python scheduled_runner_v144.py --live` | ✅ | run.env 全部参数生效 |
| ⚡ 实盘 + 自定义手数 | `python scheduled_runner_v144.py --live -a 1` | ✅ | CLI `--lots` 覆盖 run.env |
| ⚡ 实盘 + 放宽对齐条件 | `python scheduled_runner_v144.py --live --align-required 2` | ✅ | 2/3 周期一致即可入场 |
| ⚡ 实盘 + 提高入场门槛 | `python scheduled_runner_v144.py --live --min-gap 1.2` | ✅ | 缺口 ≥ 1.2 才考虑 |
| 🛡️ 强制干跑（覆盖 run.env） | `python scheduled_runner_v144.py --live --dry-run` | ❌ | 打印实盘逻辑但不下单 |

---

## 常用组合示例

### 1) 安全预览（默认）
```bash
# 使用 run.env 配置，不发单，先看信号
python scheduled_runner_v144.py
```

### 2) 实盘标准执行（你当前在用）
```bash
# 完整筛选 + 1单位 + 真实下单
python scheduled_runner_v144.py --live -a 1
```

### 3) 放宽条件，增加入场机会
```bash
# 2/3 周期对齐即可入场，降低缺口门槛
python scheduled_runner_v144.py --live --align-required 2 --min-gap 0.8
```

### 4) 激进多单模式
```bash
# 最多同时入场 2 个最强信号
python scheduled_runner_v144.py --live --max-entries 2 -a 1
```

### 5) 调试/参数试验
```bash
# 实盘参数全开但不下单，验证配置与信号
python scheduled_runner_v144.py --live --dry-run --max-entries 2 --min-gap 0.9
```

### 6) 定时调度（cron 示例）
```bash
# 每 15 分钟执行 — 使用绝对路径避免环境问题
*/15 * * * * /Users/liqin/miniforge3/envs/ai-sprint-311/bin/python \
  /Users/liqin/projects/gemini_ai/scheduled_runner_v144.py \
  --live -a 1 >> /Users/liqin/projects/gemini_ai/bot.log 2>&1
```

---

## 配置文件关系

```
run.env 优先级
├── CLI 参数（最高，优先覆盖）
├── run.env 文件值
└── 代码内置默认值（兜底）

run.env.example  →  模板参考，不生效
run.env          →  实际生效配置，已纳入 .gitignore
```

关键参数对照：

| run.env 键 | 对应 CLI 参数 |
|---|---|
| `LIVE_LOT_SIZE` | `--lots` / `-a`（CLI 优先） |
| `DEMO_LOT_SIZE` | 非 `--live` 模式默认手数 |
| `DRY_RUN` | `--dry-run` / `--no-dry-run`（CLI 优先） |
| `MIN_GAP` | `--min-gap` |
| `MAX_ENTRIES` | `--max-entries` |
| `ALIGN_REQUIRED` | `--align-required` |
| `MC_NEUTRAL_TP` | `--mc-neutral-tp` |
| `GAP_THRESHOLD` | `--gap-threshold` |
| `STRICT_ALIGN` | `--strict-align` |

---

## 返回/结果解读

### 正常信号流程
```
✅ Selected: SELL AUD_JPY (-2.3286)
✅ SIGNAL: SELL AUD_JPY @ 110.436 | SL=112.268 | TP=107.688
→ Sending order to OANDA...
```

### 市场临时休市/流动性薄
```
❌ Order NOT confirmed — MARKET_HALTED
→ 处理：等待 5–15 分钟或下一 cron 周期自动重试
→ 优化：将 FOK → IOC 可大幅减少此类拒绝
```

### 无合格信号
```
📌 Next evaluation: next scheduled cycle
→ 非错误：仅当前无满足全部条件的机会，属于正常状态
```

### 参数未生效
```
[CONFIG] ... loaded from run.env
→ 确认是否携带 --live：不带则筛选参数不生效
```

---

## 故障排查速查

| 现象 | 最可能原因 | 解决 |
|---|---|---|
| 参数加载不生效 | 忘记加 `--live` | 实盘筛选参数仅在 `--live` 下生效 |
| `MARKET_HALTED` 拒绝 | 流动性薄/短暂停 | 改用 `timeInForce='IOC'`；或等待重试 |
| 手数不对 | `--lots` 优先级最高；区分 DEMO/LIVE | 用 `-a N` 显式指定 |
| 环境/包缺失 | Python 解释器混淆 | 用 `which python` 确认路径；写全路径 |
| 重复入场 | 幂等保护未启用 | 确认 `Tag+IdemMode: ENABLED` |
| Git 提交泄露密钥 | `run.env` 未被忽略 | 确认 `.gitignore` 包含 `run.env` |

