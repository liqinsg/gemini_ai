"""
Gemini API Quota Guard
----------------------
Tracks daily Gemini API call count and auto-falls back to RULES_ONLY
when approaching the free tier limit (20 calls/day for gemini-2.5-flash).

Usage:
    from quota_guard import quota_guard

    if quota_guard.is_available():
        try:
            result = call_gemini()
            quota_guard.record_call()
        except Exception as e:
            quota_guard.handle_error(e)  # auto-detects daily exhaustion
"""

from datetime import date

DAILY_LIMIT    = 20   # free tier limit — raise if you upgrade
RESERVE_BUFFER = 3    # stop calling when this many remain

# Strings in 429 errors that mean DAILY quota (not per-minute rate limit)
DAILY_QUOTA_SIGNALS = [
    "GenerateRequestsPerDayPerProjectPerModel",
    "free_tier_requests",
    "quota exceeded",
]


def is_daily_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s.lower() in msg for s in DAILY_QUOTA_SIGNALS)


class _QuotaGuard:
    def __init__(self):
        self._date      = date.today()
        self._count     = 0
        self._exhausted = False  # True when daily 429 detected

    def _reset_if_new_day(self):
        today = date.today()
        if today != self._date:
            print(f"[QUOTA] New day ({today}). Resetting Gemini quota counter.")
            self._date      = today
            self._count     = 0
            self._exhausted = False

    def record_call(self, n: int = 1):
        """Call only after a SUCCESSFUL Gemini response."""
        self._reset_if_new_day()
        self._count += n
        remaining = DAILY_LIMIT - self._count
        print(f"[QUOTA] Gemini calls today: {self._count}/{DAILY_LIMIT} "
              f"({remaining} remaining)")

    def mark_exhausted(self):
        """
        Immediately disables all further Gemini calls this session until midnight.
        Called automatically by handle_error() on daily quota 429s.
        """
        self._exhausted = True
        self._count     = DAILY_LIMIT
        print("[QUOTA] Daily quota exhausted — Gemini disabled until midnight. "
              "All remaining cycles will use RULES_ONLY fallback.")

    def handle_error(self, exc: Exception):
        """
        Call when ANY Gemini call fails.
        Auto-detects daily quota exhaustion vs transient errors.
        """
        if is_daily_quota_error(exc):
            self.mark_exhausted()
        # transient errors (network, per-minute rate limit) are handled by retry.py

    def is_available(self) -> bool:
        self._reset_if_new_day()
        if self._exhausted:
            print("[QUOTA] Gemini quota exhausted today. Using RULES_ONLY fallback.")
            return False
        available = self._count < (DAILY_LIMIT - RESERVE_BUFFER)
        if not available:
            print(f"[QUOTA] Approaching daily limit "
                  f"({self._count}/{DAILY_LIMIT}, buffer={RESERVE_BUFFER}). "
                  f"Falling back to RULES_ONLY.")
        return available

    @property
    def calls_today(self) -> int:
        self._reset_if_new_day()
        return self._count

    @property
    def remaining(self) -> int:
        self._reset_if_new_day()
        return max(0, DAILY_LIMIT - self._count)


# Single shared instance — import this everywhere
quota_guard = _QuotaGuard()