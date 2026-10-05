✅ 集成方式总结：

### 怎么跑

```bash
# 默认 OFF —— runner 完全不受影响
python scheduled_runner_v3.py run

# 打开 observation
COMPOSITE_INDEX_LOG=1 python scheduled_runner_v3.py run

# 自定义篮子权重（DXY-like）
COMPOSITE_INDEX_LOG=1 \
COMPOSITE_BASKET_WEIGHTS='{"USD":0.30,"EUR":0.25,"GBP":0.20,"AUD":0.15,"CHF":0.10}' \
python scheduled_runner_v3.py run

# 只记录部分 target
COMPOSITE_INDEX_LOG=1 COMPOSITE_TARGETS='JPY,USD,EUR' python scheduled_runner_v3.py run

# 自定义日志路径
COMPOSITE_LOG_DIR=/data/obs python scheduled_runner_v3.py run
```

### 产出物

**1. 控制台实时面板**（每个 runner cycle 结束后打印）：

```
  ╔══════════════════════════════════════════════════════════════════╗
  ║   COMPOSITE STRENGTH INDEX  (Observation-only, V1.1)              ║
  ╠══════════════════════════════════════════════════════════════════╣
  ║ CHF  RawIndex=+1.3961  DirBal=100.0  Breadth=5/5  Rank=1/6  z=+1.437  S_CHF=+1.3276═║
  ║ JPY  RawIndex=+0.9048  DirBal= 92.3  Breadth=4/5  Rank=2/6  z=+0.850  S_JPY=+0.9182═║
  ║ GBP  RawIndex=+0.4612  DirBal= 75.0  Breadth=3/5  Rank=3/6  z=+0.415  S_GBP=+0.5485═║
  ║ AUD  RawIndex=-0.1429  DirBal= 42.2  Breadth=2/5  Rank=4/6  z=-0.127  S_AUD=+0.0451═║
  ║ USD  RawIndex=-0.1459  DirBal= 42.1  Breadth=1/5  Rank=5/6  z=-0.130  S_USD=+0.0426═║
  ║ EUR  RawIndex=-2.4733  DirBal=  0.0  Breadth=0/5  Rank=6/6  z=-4.948  S_EUR=-1.8969═║
  ╚══════════════════════════════════════════════════════════════════╝
  [COMPOSITE] appended 6 record(s) → logs/composite_observation.jsonl
```

**2. `logs/composite_observation.jsonl`**（append-only，每个 target 一行）：

```json
{"ts":"2026-10-05T02:40:18Z", "target":"JPY",
 "raw_index":0.9048, "directional_balance":92.3, "gap_score":92.3,
 "basket_mean":0.0134, "sigma_w":1.064,
 "breadth_fraction":0.8, "weighted_breadth":0.8,
 "rank":2, "rank_total":6, "z":0.850,
 "target_strength":0.9182,
 "weight_hash":"2631bc15519e", "schema_version":"1.1.0",
 "contributions":[{"c":"EUR","w":0.2,"g":2.8151,"C":0.5630},
                  {"c":"USD","w":0.2,"g":0.8756,"C":0.1751},
                  {"c":"AUD","w":0.2,"g":0.8731,"C":0.1746},
                  {"c":"GBP","w":0.2,"g":0.3697,"C":0.0739},
                  {"c":"CHF","w":0.2,"g":-0.4094,"C":-0.0819}]}
```

### 安全性（spec §13 的承诺都满足）

| 承诺                     | 实现                                                                             |
| ------------------------ | -------------------------------------------------------------------------------- |
| 跑完决策路径之后才跑     | `_run_composite_observation` 插在 `_maintain_group_positions` **之后** |
| 独立 try/except          | 全部包在一个大 try/except 里，失败只打印一行 non-fatal，继续交易                 |
| 主开关默认 OFF           | `COMPOSITE_INDEX_LOG` 不是 `on/1/true` 就直接 return                         |
| 不影响交易路径           | 不 import 任何 trading 模块，不读不写任何 bot 用的 config / state                |
| import 失败也不炸 runner | `try/except` 包住 import，失败时 `_COMPOSITE_AVAILABLE=False` 直接 return    |

### 文件清单

| 文件                                  | 改了什么                                                                                                         |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `scheduled_runner_v3.py`            | +import`json` / composite 模块；+新函数 `_run_composite_observation`（约 110 行，§13 架构）；在 4361 行调用 |
| `utils/composite_strength.py`       | **新建** — 泛化核心（任何 target、任何篮子、严格 spec V1.1 数学）                                         |
| `utils/composite_report.py`         | **新建** — 人类可读 + bot JSON + all-target 表格                                                          |
| `utils/jpy_composite_index.py`      | 原 JPY 专用版本保留（向后兼容）                                                                                  |
| `tests/test_jpy_composite_index.py` | 88 测试，全绿                                                                                                    |
