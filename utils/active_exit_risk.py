"""
ActiveExitRisk — 连续主动平仓风控识别 & 冷却期管理
核心逻辑：连续主动平仓 = 风险上升信号 → 自动触发冷却 + 提高门槛
设计原则：不禁止开仓，只提高条件 + 强制冷静期
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque, List, Optional, Literal
from collections import deque

# === 配置常量（可按需调整）
MAX_RECORDS: int = 3               # 最多记录最近N笔平仓
ACTIVE_EXIT_RATIO_HIGH: float = 1.0  # 100%主动平仓 → 高风险
ACTIVE_EXIT_RATIO_ELEVATED: float = 0.67  # ≥2/3主动 → 风险上升
COOLING_HOURS_HIGH: float = 8.0    # 高风险：H8 冷静期
COOLING_HOURS_ELEVATED: float = 4.0 # 风险上升：4小时冷却
HURDLE_MULTIPLIER_HIGH: float = 1.5  # 高风险：门槛×1.5
HURDLE_MULTIPLIER_ELEVATED: float = 1.3  # 风险上升：门槛×1.3

EXIT_TYPES = Literal["MANUAL", "STOP_LOSS", "TAKE_PROFIT", "SYSTEM"]


@dataclass
class ExitRecord:
    """平仓记录：类型 + 时间"""
    exit_type: EXIT_TYPES
    timestamp: float = field(default_factory=time.time)


class ActiveExitRisk:
    """
    跟踪最近平仓记录 → 计算主动平仓占比 → 判定风险等级 → 管理冷却期
    典型用法：
        - 每次平仓后调用 .record_exit(exit_type)
        - 开仓前查询 .can_open() / .get_hurdle_multiplier()
    """

    def __init__(self, max_records: int = MAX_RECORDS) -> None:
        self._history: Deque[ExitRecord] = deque(maxlen=max_records)
        self._cooling_until: float = 0.0  # 冷却结束时间戳

    def record_exit(self, exit_type: EXIT_TYPES) -> None:
        """
        记录一笔平仓 → 自动更新风险等级 & 冷却期
        在平仓事件发生时调用
        """
        self._history.append(ExitRecord(exit_type=exit_type))
        self._update_risk_state()

    def _update_risk_state(self) -> None:
        """根据历史记录计算风险状态 → 设置冷却期"""
        if not self._history:
            self._cooling_until = 0.0
            return

        total = len(self._history)
        active_count = sum(
            1 for rec in self._history if rec.exit_type == "MANUAL"
        )
        active_ratio = active_count / total

        now = time.time()

        if active_ratio >= ACTIVE_EXIT_RATIO_HIGH and active_count >= 2:
            # 连续2单及以上 100%主动平仓 → H8 高风险冷却
            self._cooling_until = now + COOLING_HOURS_HIGH * 3600

        elif active_ratio >= ACTIVE_EXIT_RATIO_ELEVATED:
            # ≥2/3 主动平仓 → 4小时风险上升冷却
            self._cooling_until = now + COOLING_HOURS_ELEVATED * 3600

        else:
            # 风险正常 → 不清空已有冷却（避免刚触发就失效）
            pass

    @property
    def in_cooling(self) -> bool:
        """是否处于冷却期"""
        return time.time() < self._cooling_until

    @property
    def cooling_remaining_hours(self) -> float:
        """剩余冷却时间（小时）"""
        if not self.in_cooling:
            return 0.0
        return max(0.0, (self._cooling_until - time.time()) / 3600)

    def get_hurdle_multiplier(self) -> float:
        """
        获取开仓门槛乘数（与 PostExitGate 配合使用）
        返回 ≥1.0 → 越大越难通过
        """
        if not self._history:
            return 1.0

        total = len(self._history)
        active_count = sum(
            1 for rec in self._history if rec.exit_type == "MANUAL"
        )
        active_ratio = active_count / total

        if active_ratio >= ACTIVE_EXIT_RATIO_HIGH and active_count >= 2:
            return HURDLE_MULTIPLIER_HIGH
        elif active_ratio >= ACTIVE_EXIT_RATIO_ELEVATED:
            return HURDLE_MULTIPLIER_ELEVATED
        return 1.0

    def can_open(self, *, ignore_cooling: bool = False) -> bool:
        """
        综合判断是否可以开仓
        - ignore_cooling=True：仅返回乘数逻辑，不检查冷却期
        """
        if not ignore_cooling and self.in_cooling:
            return False
        return True

    def get_status(self) -> dict:
        """获取完整状态 → 用于日志/报告展示"""
        active_count = sum(
            1 for rec in self._history if rec.exit_type == "MANUAL"
        )
        total = len(self._history)
        return {
            "in_cooling": self.in_cooling,
            "cooling_remaining_hours": round(self.cooling_remaining_hours, 2),
            "active_exits": active_count,
            "total_exits": total,
            "active_ratio": round(active_count / total if total else 0.0, 2),
            "hurdle_multiplier": self.get_hurdle_multiplier(),
            "can_open": self.can_open(),
        }

    def clear_cooling(self) -> None:
        """手动提前解除冷却 —— 谨慎使用"""
        self._cooling_until = 0.0

    def clear_history(self) -> None:
        """清空历史记录（新交易日/重置时用）"""
        self._history.clear()
        self._cooling_until = 0.0


# === 快捷实例（全局共用） ===
_risk_tracker = ActiveExitRisk()

def record_exit(exit_type: EXIT_TYPES) -> None:
    """全局快捷：记录平仓事件"""
    _risk_tracker.record_exit(exit_type)

def can_open() -> bool:
    """全局快捷：是否可开仓（含冷却检查）"""
    return _risk_tracker.can_open()

def get_hurdle_multiplier() -> float:
    """全局快捷：获取门槛乘数"""
    return _risk_tracker.get_hurdle_multiplier()

def get_status() -> dict:
    """全局快捷：获取完整状态"""
    return _risk_tracker.get_status()


if __name__ == "__main__":
    # === 演示：模拟今天的连续主动平仓场景 ===
    print("=" * 60)
    print("📊 ActiveExitRisk — 连续主动平仓风控 · 演示")
    print("=" * 60)

    tracker = ActiveExitRisk()

    # 模拟：连续2笔主动平仓
    tracker.record_exit("MANUAL")
    tracker.record_exit("MANUAL")

    status = tracker.get_status()
    print(f"✅ 记录：连续 {status['active_exits']} 笔主动平仓")
    print(f"⚠️  主动平仓占比：{status['active_ratio']:.0%}")
    print(f"🔒 冷却期：{'生效' if status['in_cooling'] else '未触发'}")
    print(f"⏳ 剩余冷却：{status['cooling_remaining_hours']:.1f} 小时")
    print(f"📐 门槛乘数：×{status['hurdle_multiplier']}")
    print(f"🚫 可开仓？：{'否' if not status['can_open'] else '是'}")
    print("\n💡 结论：触发 H8 冷静期 + 门槛×1.5，不禁止但显著提高条件 ✅")
    print("=" * 60)