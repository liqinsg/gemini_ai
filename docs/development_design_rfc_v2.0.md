
# 📘 JPY强度交易系统 · 模块化架构规范 RFC v2.0

**版本**: v2.0 | **日期**: 2026-09-10 | **状态**: 正式规范 · 实施中

---

## 📑 目录

1. [核心设计原则](#一核心设计原则)
2. [参数分类与边界](#二参数分类与边界)
3. [完整文件架构](#三完整文件架构)
4. [模块职责与接口规范](#四模块职责与接口规范)
5. [标准数据流](#五标准数据流)
6. [风控集成约定](#六风控集成约定)
7. [MC读取器扩展约定](#七mc读取器扩展约定)
8. [通知扩展约定](#八通知扩展约定)
9. [向后兼容与迁移](#九向后兼容与迁移)

---

## 一、核心设计原则

### 1.1 职责单一、互不越界

> **谁的活谁干、谁的数据谁管、谁的凭证谁藏、谁也不替别人做决定。**

- 配置层：只存值、不计算、不查表、不执行业务逻辑
- 装配层：只合并查表、输出标准参数、不读文件、不连API
- 决策层：纯逻辑、输入完整参数+信号、输出明确决定、**无任何IO**
- 风控层：评估风险、返回建议/限制/参数、**不直接下单、不强制执行**
- 执行层：只干活、不决策、不持有凭证、不修改参数
- 编排层(Runner)：唯一持凭证、唯一读命令行、唯一拿实时数据、**汇总各方意见做最终决定、只传令不写逻辑**

### 1.2 接口不变、内部可换

> 标准接口签名永远不变。**内部实现、数据源、通知渠道、执行后端——随时可插拔替换，调用方一行不用改。**

### 1.3 静态/动态严格分离

> **静态参数 = 运行期间固定不变；动态参数 = 每次扫描实时获取。永远不混在一起传。**

---

## 二、参数分类与边界

### ✅ 静态参数（运行期间固定）

| 类别       | 存放位置                          | 说明                                                             |
| ---------- | --------------------------------- | ---------------------------------------------------------------- |
| 业务常数   | `config.py`                     | 规则表、系数、路径、预设值                                       |
| OANDA连接  | `config_oanda.py`               | API密钥、账户ID、环境 →**独立全局可用、不依赖业务config** |
| MC衍生静态 | MC离线生成 →`mc_loader.py`读取 | 当日不变、运行中不重算                                           |

### ✅ 动态参数（每次扫描实时获取）

> 价格、K线对齐数、强度排名、持仓状态、时间戳 → **仅Runner实时采集，向下层完整传递，下层不主动获取**

---

## 三、完整文件架构

```
gemini_ai/
├── config.py                  ← 业务静态参数（规则/系数/路径）
├── config_oanda.py            ← OANDA专属（密钥/账户/环境）独立全局可用
│
├── mc_loader.py               ← MC统一读取入口（本地JSON→将来一键切网络）
├── mc_generator.py            ← MC离线生成器（独立定时运行、不动）
│
├── config_loader.py           ← 参数装配中心：查表+合并+计算→输出标准参数包
│
├── trading_decision.py        ← 决策单元：参数+信号→开仓决定/TP/筛选
│
├── utils/
│   ├── risk_integration.py    ← 风控评估：返回建议参数/限制→不直接下单
│   ├── dynamic_risk_manager.py   ← 风控状态管理
│   └── oanda_execution.py     ← OANDA执行底层实现
│
├── execution.py               ← 执行接口层：标准place_order→可插拔实现
├── messenger.py               ← 统一通知出口：Telegram/日志/其他→统一入口
├── record_keeper.py           ← 统一记录单元：日志/JSONL/审计→统一出口
│
└── scheduled_runner_v2.py     ← 编排/传令官：汇总→最终决定→派活→记结果
```

---

## 四、模块职责与接口规范

### 4.1 config.py —— 业务静态参数库

> **只存值、不计算、不查表、不写逻辑**

```python
# 交易预设规则表
SAFE_ZONE_HALF_WIDTH = 0.38

REGIME_PRESET = {
    "NEUTRAL":       {"align_min": 2, "tp_mult": 1.0, "sl_mult": 1.0, "max_count": 1},
    "CONSOLIDATION": {"align_min": 3, "tp_mult": 0.8, "sl_mult": 1.2, "max_count": 1},
    "STRONG_MOMENTUM":{"align_min": 2, "tp_mult": 1.3, "sl_mult": 0.9, "max_count": 2},
}

EDGE_OVERRIDE =     {"align_min": 3, "tp_mult": 0.8, "sl_mult": 1.0, "max_count": 1}
MC_RESULT_PATH = "./mc_results/daily"
```

### 4.2 config_oanda.py —— OANDA专属配置

> **独立文件、全局可import、不掺和业务参数**

```python
OANDA_ENV = os.getenv("OANDA_ENV", "practice")
OANDA_API_TOKEN = os.getenv("OANDA_API_TOKEN", "")
OANDA_ACCOUNTS = {
    "demo1": "101-xxx-xxx-001",
    "demo2": "101-xxx-xxx-002",
}
```

### 4.3 mc_loader.py —— MC数据读取入口

> **统一接口、今天读本地JSON、将来一键切网络源、调用方不用改**

```python
def load_mc_raw(pair: str) -> dict | None:
    """
    输入: "USD_JPY"
    输出: {pair, regime, band_low, band_high, prob_up, prob_down}
    ⚠️ 仅读取、原样返回、不计算、不查表、不决策
    实现可切换: local_json / oanda_api / yahoo —— 调用方永远同一接口
    """
```

### 4.4 config_loader.py —— 参数装配中心

> **唯一规则映射点：查表、合并、计算、补全默认值**

```python
def assemble_params(mc_raw: dict, current_price: float) -> dict:
    """
    输入: MC原始数据 + 当前实时价
    流程: 中心点→保守区间→查表→边缘覆盖
    输出: 标准运行参数包
    {
        "pair", "regime", "zone": "SAFE"|"EDGE",
        "mc_band": [lo, hi], "center_price", "safe_range": [lo, hi],
        "align_min": int, "tp_mult": float, "sl_mult": float, "max_count": int
    }
    """
```

### 4.5 trading_decision.py —— 决策单元

> **纯函数、无IO、可独立测试、内部可替换、接口永远不变**

```python
def evaluate_entry(run_params: dict, signal_data: dict) -> dict:
    """
    输入: 标准参数包 + 实时信号{aligned_count, strength_rank, trend_dir}
    输出: {should_trade, reason, tp_mult, sl_mult, priority, candidates}
    """
```

### 4.6 风控模块（现有不动）

> **评估后返回建议 → 不直接下单 → Runner统筹做最终决定**

```python
# ⚠️ 重要约定：风控只返回建议、不强制执行、不直接下单
def evaluate_risk(pair: str, decision: dict) -> dict:
    """
    返回: {allowed: bool, reason: str, override_units, override_tp_mult, ...}
    Runner拿到风控返回 → 综合决策 → 再交execution执行
    """
```

### 4.7 execution.py —— 执行接口层

> **标准接口、不持有凭证、由Runner传入认证信息**

```python
def place_order(decision: dict, auth: dict, account_id: str, dry_run: bool) -> dict:
    """
    标准接口永远不变
    auth = {"token": "...", "env": "practice/live"} —— Runner传入、本层不留存
    内部可插拔: mock / oanda_v1 / future_version
    """
```

### 4.8 messenger.py —— 统一通知出口

> **不限于Telegram、将来可扩展多渠道、调用方不用改**

```python
def send_notice(level: str, title: str, content: dict) -> None:
    """
    level: info/warn/error/critical
    输出渠道: Telegram / 日志 / 其他 —— 调用方不用关心实现
    """
```

### 4.9 record_keeper.py —— 统一记录单元

> **任何地方记日志 → 走这里、不直接写文件**

```python
def log_event(category: str, payload: dict) -> None:
    """统一日志/JSONL/审计入口"""

def record_outcome(decision: dict, result: dict) -> None:
    """交易结果专项归档"""
```

### 4.10 scheduled_runner_v2.py —— 编排/传令官

> **唯一持凭证、唯一读命令行、唯一拿实时数据、汇总各方做最终决定、不写业务规则**

```
流程：
1. 读命令行参数 → account / dry_run
2. 从config_oanda组装auth → 全程不读密钥文件
3. 扫描合格品种
   ├─ mc_loader.load_mc_raw(pair)
   ├─ 采集实时动态信号: price, aligned_count, strength_rank
   ├─ config_loader.assemble_params(MC, price) → 标准参数包
   ├─ trading_decision.evaluate_entry(参数, 信号) → 开仓决定
   ├─ 风控模块.evaluate_risk(决定) → 风控建议/限制
   ├─ ✅ Runner综合: 决策+风控 → 最终决定
   ├─ execution.place_order(最终决定, auth, account, dry_run)
   ├─ messenger.send_notice(结果)
   └─ record_keeper.log_event(参数+决定+风控+结果)
4. ❗ 全程不写规则、不算区间、不查表、不硬编码逻辑
```

---

## 五、标准数据流

```
config.py ───┐
config_oanda ─┼─→ config_loader ──→ 标准参数包 ──┐
mc_loader ────┘                                   │
                                                  ▼
                                       trading_decision 评估开仓
                                                  │
                                                  ▼
                                       风控模块 评估风险 → 返回建议
                                                  │
                                                  ▼
        messenger ←─────── 最终决定 ←─────── Runner 统筹汇总
           │                                    │
           ▼                                    ▼
      通知出口                           execution 执行接口
                                                  │
                                                  ▼
                                         record_keeper 统一记录
```

---

## 六、风控集成约定

> **现有风控模块完全保留、继续独立工作、不修改内部代码。**

- ✅ 风控**返回建议参数/限制/可否开仓**给Runner
- ✅ Runner拿到「决策+风控」→ **做最终统筹决定**
- ✅ 风控**不直接下单、不覆盖执行、不持有凭证**
- ✅ 风控内部逻辑、状态管理、数据结构 → **完全不动**

---

## 七、MC读取器扩展约定

> **mc_loader.py 接口签名永久不变 → 底层数据源随时可换**

- 第一版：读取本地JSON（`utils/mc_loader_local.py` 现有代码迁入）
- 下一版：加网络源 → 改mc_loader内部 → **调用方一行不用改**
- 约定：返回字段名/格式 **永远一致** → 上层不感知数据源变化

---

## 八、通知扩展约定

> **messenger.py 统一出口 → 不限于Telegram、随时扩展渠道**

- 第一版：Telegram为主
- 下一版：加日志/监控/其他 → 改messenger内部 → **调用方不用改**
- 约定：`send_notice(level, title, content)` 签名永远不变

---

## 九、向后兼容与迁移

| 原则          | 说明                                        |
| ------------- | ------------------------------------------- |
| v1.3 不动     | 继续独立运行、互不影响                      |
| v1.4 参考拆分 | 现有逻辑逐步迁入各独立模块                  |
| 接口优先      | 先定标准接口 → 再逐步迁移内部实现          |
| 可回退        | 任一模块异常 → 降级回原有逻辑、不中断运行  |
| 不破坏现有MC  | mc_generator / 结果JSON格式 → 完全兼容不动 |

---

## ✅ 一句话总结

> **配置分开、参数装配、决策独立、风控建议、执行插拔、通知统一、Runner只管传令与最终统筹——接口定死、内部随便换、越往后越轻松！** ✅

---

RFC v2.0 文档定稿！接下来要不要我**按这份规范逐个生成独立模块代码**？从哪个开始你说了算！🚀
