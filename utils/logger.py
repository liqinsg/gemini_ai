"""
utils/logger.py

兼容层：历史代码可能仍会 import 这里的 setup_logger()/logger。
铁律：日志配置只能在 utils/logging_utils.py 统一完成，其他任何文件禁止 basicConfig/addHandler。
"""

from .logging_utils import get_logger


def setup_logger(name: str = "fx_bot", log_dir: str | None = None):
    # log_dir 参数已废弃，仅保留签名兼容
    return get_logger(name)


logger = get_logger(__name__)
