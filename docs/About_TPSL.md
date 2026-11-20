ChatGPT：
**现在的TP设置暂时不支持分级TP。“减一部分后，让剩余仓位跑到周线目标”。**

### 当前 TP 实际怎么工作
1，v3 用 **D（日线）ATR** 

owner's comment： 中长线应该是周线？

算初始 SL/TP，尾仓默认 TP 已设为 **6R**；正常波动下 SL 约为 2.2 倍日 ATR，因此 TP 约为 13.2 倍日 ATR。尾仓采用固定 R 型大目标，不再被中途的 2R/日线回撤目标提前全平。

2, 配置里的分批梯级是 **1R 平原始仓位的 50%，6R 再平剩余尾仓**。

- 到 1R：减半；
- 到 6R：剩余尾仓被平掉。

3, 是说，现有设置会分两次退出，但**没有留下跑周线的尾仓**。另外，`config_bot` 里的 `ATR_TP_MULT` 是 2.5 或 3.0，但 v3 的策略实际读取的是 `config.py` 的 `ATR_RR_MULTIPLE=2.0`，那些 profile 值目前没有控制这条 TP 计算路径。
my comment：should we change this to a bigger value?

### 我建议的结构

| 阶段 | 仓位 | 处理 |
|---|---:|---|
| 首仓 | 10,000 units | 标准仓；按策略初始 SL 保护 |

my comment: demo标准仓暂定为 10,000 units
lieve 仓暂定为 4 units

| 到 1R | 减仓至 5,000 or 1/2 of the original position | 锁定部分利润 |
| 剩余仓位 | 5,000 or 1/2 of the original position | TP 放到周线目标，或交给周线失效条件出场 |
| 首仓完全止盈后再次出现信号 | 5,000 or 1/2 of the original position | 下一笔仓位减半 |
| 再次止盈后出现信号 | 2,500 units | 第三阶段；再下一次不超过此档 |

**优先用同一笔仓位做 reduce，再让剩余仓位跑远**，更容易控制。但要增加一个reduce的操作。

TP 后不歇息。继续寻找下一笔信号，仓位由MC（当前是D MC，应该结合W，M 数据。不过MC的regime有提示风险consolidation, neuture or strong momentum。采用哪个好呢？）当前周期扫描机制可以做到：仓位平掉后，下一轮只要信号仍合格，就能重新进场。但现在没有“上一笔 TP 后下一笔减半”的阶段记录，仓位也不会自动按 10,000 → 5,000 → 2,500 递减；目前只有按行情延伸度等因素动态缩小仓位的逻辑。
my comment: don't Cooldown 

还有两个和仓位控制有关的细节：

- runner 默认每轮最多执行一笔；全局最大持仓数则会随市场状态变化。相同货币对的持仓未平时会被拦截，因此现在不会在同一对上叠加第二仓。
my comment: 考虑当被套的时候。如果满足开仓条件。如果满仓（标准仓位），则不动。若少于标准仓位，则开同方向新仓。若反向则主动关仓

- 代码传给 OANDA 的是 **units**。10,000 units for demo and 4 units for lieve 
my comment: demo标准仓暂定为 10,000 units，lieve 仓暂定为 4 units由命令行参数和run.env控制

当前保留的**弱势出场**震荡或亏损时主动平掉组内支撑分最低的仓位。但只平相关的仓位。例如最弱的是gbpusd, 而open position是EURUSD。则不动


整体方向清楚了，尤其是把“同一笔减仓”和“止盈后下一笔缩小仓位”分开考虑。实现前建议再明确几条规则，避免减仓后又被自动加回，或在震荡时因相对排名出场。

### 周线 TP：不建议只把 2R 调大

目前初始 SL/TP 用 **D（日线）ATR**，尾仓 `ATR_RR_MULTIPLE=6.0`；周线支撑阻力主要用于入场过滤，不决定固定 6R TP。固定 6R 的作用是给尾仓明确的大目标，同时 1R 自动 reduce。

如果持仓目标是周级别，我建议 **D 用于入场时机，W 用于尾仓 TP 或趋势失效判断**。1R 时减掉一半，剩余一半挂到周线目标；别再让它保留当前的 2R broker TP，否则到 2R 还是会全平。具体 TP 倍数最好根据策略历史表现确定，不宜只凭“周线持仓”直接猜一个更大的数字。

### 采用 reduce，还是多张不同 TP？

按你描述的 10,000 → 5,000 → 2,500，我建议先做 **单笔仓位分批 reduce**：

1. 首笔按标准仓开仓。
2. 到 1R，reduce 一半。
3. 剩余仓位跑到 W 目标。
4. 整笔交易完全止盈后，下一笔合格信号用半仓；再完整止盈后用四分之一仓。

现有分批逻辑已经配置了 1R 减 50%、2R 再减 50%，但第二级会与当前 2R broker TP 一起结束剩余仓位，所以目前没有周线尾仓。OANDA 的 Trade API 支持按指定 units 部分平仓；这条路径可以沿用。[OANDA Trade endpoint](https://developer.oanda.com/rest-live-v20/trade-ep/)

### TP 后继续交易、MC 和仓位规则

- **不设置冷却**是可行的：TP 后下一轮照常扫描信号，合格就重进。当前 runner 本身没有入场冷却流程，但“TP 后阶段”还没有状态记录，所以 5,000/2,500 的递减仓位需要另加阶段管理。
- **MC 建议按周期分工，而不是简单投票混成一个结果**：D 决定这轮能否入场；W 决定是否允许加到第二、第三阶段；M 若以后有可靠数据，只作宏观风险否决。当前 runner 的 regime 是对各货币对的 **D MC** 做多数决策；数据接口有 D/W，尚无对应的 M MC 路径。([get_mc_data.py](/home/qili/projects/gemini_ai/get_mc_data.py:60), [scheduled_runner_v3.py](/home/qili/projects/gemini_ai/scheduled_runner_v3.py:2577))
- **被套后同向加仓**属于明确的加风险操作。建议只有出现新的完整入场信号才加，并限制同一货币对的总 units；若总量已达当前阶段上限就不动。若信号反向，先关掉该货币对 bot-owned 仓位并确认已平，再等下一轮确认反向信号后开新方向。现在代码会记录反向候选，但执行层仍会因该 pair 已有仓位而拦截，尚未实现“反向主动关仓”。[scheduled_runner_v3.py](/home/qili/projects/gemini_ai/scheduled_runner_v3.py:4628)
- **10,000 demo / 4 live**可以通过 `--lots` 或 `run.env` 控制；它们是 OANDA 的 units 数，不是标准手数。CLI 参数优先于环境变量，环境变量优先于配置默认值。OANDA 的 API 也按 Trade units 指定部分平仓数量。[scheduled_runner_v3.py](/home/qili/projects/gemini_ai/scheduled_runner_v3.py:6535) [OANDA Trade endpoint](https://developer.oanda.com/rest-live-v20/trade-ep/)
- **弱势出场仍可能在震荡时平仓**：当前它按报价货币分组，只在实际持有的仓位中挑支撑分最低者，不会因为 GBPUSD 弱就去平一个并未被挑中的 EURUSD；但若某组只有一笔符合年龄条件的仓位，它也可能把那笔选为最弱，而且目前没有盈亏门槛。若你希望震荡仓继续持有，最好给弱势出场再加绝对失效条件，而不是只按相对排名选“最弱”。

另一个需要定清楚的是**阶段何时递增或重置**。我建议只有整笔仓位完成 TP 才进入下一档；SL、反向平仓或策略失效则重置为标准仓。并且“被套补仓”应有独立上限，不能把已在 1R 减掉的那半仓又自动补回。

另外，初始止损若改成周线级别，止损距离会变宽；units 不能仍机械固定为 10,000/4，否则单笔风险金额也会变大。建议把这些数作为仓位上限，再按止损距离做风险封顶。