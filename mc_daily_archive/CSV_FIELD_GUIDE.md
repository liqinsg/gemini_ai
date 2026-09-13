# CSV 字段说明文档

## 📋 字段总览
| 字段 | 说明 |
|---|---|
| pair | 货币对 |
| current_price | 当前价格(查询时点) |
| mc_range_low/high | MC90%区间上下沿 |
| mc_width_price | MC区间宽度(价格) |
| mc_up_pct / mc_down_pct | MC上涨/下跌概率(0–100) |
| p2_range_low/high | P2收敛区间上下沿 |
| p2_width_price | P2区间宽度(价格) |
| edge_pct | 涨跌差距=上涨%−下跌% |
| edge_distance_pct | 现价距最近P2边缘距离(%) |
| edge_warning | 贴边风控: True=暂不入场 |
| signal | 最终信号:LONG/SHORT/NEUTRAL |
| sl_est / tp_est | 预估止损/止盈(参考值) |
| width_change_pct | 区间较昨日变化(%) |
| market_regime | 市场状态 |
| profile | P1/P2/P3 |
| generated_utc | 生成时间 |

## 🔒 风控规则(Decision单元)
- edge_distance_pct < 0.15% → 贴边 → 直接观望不入场
- 理由：止损空间不足、盈亏比差、易假突破

## 📈 SL/TP预估逻辑(P2区间为基准)
- 看多: SL=P2下界×0.999 / TP=P2上沿
- 看空: SL=P2上界×1.001 / TP=P2下界
- 观望: 不预估SL/TP

## 🧪 运行模式
- MC-only 纯观察：仅MC概率+P2门槛，不加其他策略
- 先跑2–3天收集数据验证胜率
