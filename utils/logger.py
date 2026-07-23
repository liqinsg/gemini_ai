# utils/logger.py
import logging
import os
from datetime import datetime


def setup_logger(name="trading_system", log_dir="logs"):
    """
    Create a logger that writes to both console and file
    File name includes date for easy archiving
    """
    # Create logs folder if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)

    # Create log file name with current date
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"{name}_{today}.log")

    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # Avoid duplicate logs

    # Format: timestamp | level | message
    log_format = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_format)

    # File handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(log_format)

    # Add handlers
    if not logger.handlers:
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    return logger


# Create global logger instance
logger = setup_logger()