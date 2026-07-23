"""
Retry utility
-------------
Wraps any callable with configurable retries, delay, and backoff.
Covers: network errors, API rate limits, timeouts, and empty responses.
"""

import time
import functools


# Error substrings that indicate a retriable condition
RETRIABLE_SIGNALS = [
    "rate limit", "too many requests", "429",
    "timeout", "timed out", "connection",
    "network", "service unavailable", "503", "502", "500",
    "quota", "resource exhausted",
]

# Errors that are permanent — no point retrying
FATAL_SIGNALS = [
    "invalid api key", "unauthorized", "403",
    "insufficient funds", "billing", "payment",
    "no such model", "invalid model",
]


def is_retriable(exc: Exception) -> bool:
    msg = str(exc).lower()
    if any(s in msg for s in FATAL_SIGNALS):
        return False
    if any(s in msg for s in RETRIABLE_SIGNALS):
        return True
    return True  # default: retry unknown errors


def with_retry(fn, *args, max_attempts: int = 3, delay: float = 5.0,
               backoff: float = 2.0, label: str = "", **kwargs):
    """
    Calls fn(*args, **kwargs) up to max_attempts times.
    Waits delay seconds between attempts, doubling each time (backoff).
    Returns the result on success, or raises the last exception on final failure.

    label: optional name shown in log messages (defaults to fn.__name__)
    """
    name = label or getattr(fn, "__name__", str(fn))
    last_exc = None
    wait = delay

    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if not is_retriable(e):
                print(f"  [RETRY] {name} — fatal error, not retrying: {e}")
                raise

            if attempt < max_attempts:
                print(
                    f"  [RETRY] {name} — attempt {attempt}/{max_attempts} failed: {e}")
                print(f"  [RETRY] Waiting {wait:.0f}s before retry...")
                time.sleep(wait)
                wait *= backoff
            else:
                print(
                    f"  [RETRY] {name} — all {max_attempts} attempts failed. Giving up.")

    raise last_exc


def retryable(max_attempts: int = 3, delay: float = 5.0, backoff: float = 2.0):
    """
    Decorator version.

    @retryable(max_attempts=3, delay=5)
    def my_api_call():
        ...
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return with_retry(fn, *args,
                              max_attempts=max_attempts,
                              delay=delay,
                              backoff=backoff,
                              **kwargs)
        return wrapper
    return decorator
