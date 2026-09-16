"""Compatibility surface for the retired disk-backed dynamic risk manager.

Runtime trade state is now owned exclusively by OANDA. The active scheduler
uses native stop-loss/take-profit orders and does not restore local clusters.
"""
from __future__ import annotations

from typing import Dict, List, Optional


ENABLE_DYNAMIC_RISK_MANAGER = False


class RiskIntegrationError(RuntimeError):
    """Raised when obsolete cluster-persistence APIs are invoked."""


def manage_open_positions(strength_matrix: Optional[Dict[str, float]] = None) -> List[str]:
    """Retained for older callers; no local state is read or written."""
    return []


def enforce_global_invalidation_sweep(
    strength_matrix: Optional[Dict[str, float]] = None,
) -> List[str]:
    """Retained for older callers; stateful risk management is disabled."""
    return []


def _retired(*_args, **_kwargs):
    raise RiskIntegrationError(
        "Disk-backed cluster management was retired. Use OANDA live state and native orders."
    )


list_managed_instruments = _retired
load_cluster_data = _retired
save_cluster_data = _retired
delete_cluster_data = _retired
restore_cluster = _retired
new_cluster_from_fill = _retired
