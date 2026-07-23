# utils/strategy_helpers.py
import contextlib
import json
import os
import time
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional, Union

import oandapyV20.endpoints.instruments as instruments
from google import genai
from google.genai import types

import config as _config
from config import (
    SIGNAL_TIMEFRAMES, SL_BUFFER_PIPS, SPREAD_PIPS,
    CURRENCIES, STRENGTH_PAIRS, STRENGTH_TIMEFRAMES,
    STRENGTH_FAST_LOOKBACK, STRENGTH_SLOW_LOOKBACK,
    STRENGTH_FAST_WEIGHT, STRENGTH_SLOW_WEIGHT,
    ENABLE_STRENGTH_ACCELERATION, STRENGTH_ACCELERATION_WEIGHT, STRENGTH_ATR_PERIOD,
    ENABLE_EMA_TREND, ENABLE_ATR_NORMALIZED_STRENGTH,
    ENABLE_BREAKOUT_CONFIRMATION, BREAKOUT_CONFIRMATION_CLOSES,
    ENABLE_ATR_SLTP,
    ENABLE_NEWS_FILTER, NEWS_LOG_PATH, NEWS_CURRENCIES,
    GEMINI_NEWS_MODEL, GEMINI_NEWS_FALLBACK_MODEL,
    DOMINANCE_ATR_PERIOD, SIGNAL_TIMEFRAMES 
)

# --- Account ID safe lookup ---
OANDA_ACCOUNT_ID = getattr(_config, "OANDA_ACCOUNT_ID", None) or os.getenv("OANDA_ACCOUNT_ID")
if not OANDA_ACCOUNT_ID:
    print("[HELPERS] WARNING: OANDA_ACCOUNT_ID not found in config.py or environment.")


# ==========================================
# MARKET DATA HELPERS
# ==========================================
def get_candles(instrument: str, granularity: str, count: int) -> list:
    from utils import oanda_client
    params = {"count": count, "granularity": granularity}
    try:
        req = instruments.InstrumentsCandles(instrument=instrument, params=params)
        oanda_client.request(req)
        return [c for c in req.response.get("candles", []) if c["complete"]]
    except Exception as e:
        print(f"  [HELPERS] Candle fetch failed {instrument} {granularity}: {e}")
        return []


def _atr_from_candles(candles: List[dict], period: int) -> Optional[float]:
    if len(candles) < period + 1:
        return None

    true_ranges = []
    prev_close = float(candles[0]["mid"]["c"])
    for c in candles[1:]:
        high = float(c["mid"]["h"])
        low = float(c["mid"]["l"])
        close = float(c["mid"]["c"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
        prev_close = close

    if len(true_ranges) < period:
        return None
    return sum(true_ranges[-period:]) / period


def get_atr_with_volatility_context(
    instrument: str, period: int = 14, history_lookback: int = 50
) -> Tuple[Optional[float], Optional[float]]:
    candles = get_candles(instrument, "D", count=period + history_lookback + 5)
    if len(candles) < period + 2:
        return None, None

    true_ranges = []
    prev_close = float(candles[0]["mid"]["c"])
    for c in candles[1:]:
        high = float(c["mid"]["h"])
        low = float(c["mid"]["l"])
        close = float(c["mid"]["c"])
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close

    if len(true_ranges) < period:
        return None, None

    atr_series = [
        sum(true_ranges[i - period:i]) / period
        for i in range(period, len(true_ranges) + 1)
    ]
    current_atr = atr_series[-1]

    history = atr_series[:-1][-history_lookback:]
    if len(history) < 10:
        return current_atr, None

    mean = sum(history) / len(history)
    variance = sum((x - mean) ** 2 for x in history) / len(history)
    std = variance ** 0.5
    z_score = (current_atr - mean) / std if std > 0 else 0.0
    return current_atr, z_score


def get_dominance_normalizer(pair: str) -> Optional[float]:
    candles = get_candles(pair, "D", count=DOMINANCE_ATR_PERIOD + 5)
    return _atr_from_candles(candles, DOMINANCE_ATR_PERIOD)


def get_pair_momentum(instrument: str, granularity: str) -> Optional[float]:
    needed = STRENGTH_SLOW_LOOKBACK + STRENGTH_ATR_PERIOD + 5
    candles = get_candles(instrument, granularity, needed)
    if len(candles) < STRENGTH_SLOW_LOOKBACK + 1:
        return None

    closes = [float(c["mid"]["c"]) for c in candles]
    latest = closes[-1]
    fast_close = closes[-1 - STRENGTH_FAST_LOOKBACK]
    slow_close = closes[-1 - STRENGTH_SLOW_LOOKBACK]

    fast_move = latest - fast_close
    slow_move = latest - slow_close

    if ENABLE_ATR_NORMALIZED_STRENGTH:
        atr_period = min(STRENGTH_ATR_PERIOD, len(candles) - 1)
        atr = _atr_from_candles(candles, atr_period)
        if atr and atr > 0:
            fast_component = fast_move / atr
            slow_component = slow_move / atr
        else:
            fast_component = fast_move / fast_close * 100
            slow_component = slow_move / slow_close * 100
    else:
        fast_component = fast_move / fast_close * 100
        slow_component = slow_move / slow_close * 100

    blended = fast_component * STRENGTH_FAST_WEIGHT + slow_component * STRENGTH_SLOW_WEIGHT

    if ENABLE_STRENGTH_ACCELERATION:
        acceleration = fast_component - slow_component
        blended += acceleration * STRENGTH_ACCELERATION_WEIGHT

    return blended


def build_strength_matrix() -> Dict[str, float]:
    scores = {c: 0.0 for c in CURRENCIES}
    samples = {c: 0 for c in CURRENCIES}

    for pair in STRENGTH_PAIRS:
        parts = pair.split("_")
        if len(parts) != 2:
            continue
        base, quote = parts
        if base not in CURRENCIES or quote not in CURRENCIES:
            continue

        for granularity, weight in STRENGTH_TIMEFRAMES.items():
            momentum = get_pair_momentum(pair, granularity)
            if momentum is None:
                continue
            scores[base] += momentum * weight
            scores[quote] -= momentum * weight
            samples[base] += 1
            samples[quote] += 1

    for c in CURRENCIES:
        if samples[c] > 0:
            scores[c] /= samples[c]

    return scores


def format_strength_ranking(scores: Dict[str, float]) -> str:
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    lines = ["  Currency Strength Ranking:"]
    for i, (currency, score) in enumerate(ranked, 1):
        bar = "█" * min(int(abs(score) * 20), 40)
        direction = "▲" if score > 0 else "▼"
        lines.append(f"  {i}. {currency}: {score:+.4f} {direction} {bar}")
    gap = ranked[0][1] - ranked[-1][1]
    lines.append(f"\n  Score gap: {gap:.3f} "
                 f"({'STRONG' if gap > 1.5 else 'MODERATE' if gap > 0.5 else 'COILING'})")
    return "\n".join(lines)


def get_ma5_position(instrument: str, granularity: str) -> Optional[str]:
    candles = get_candles(instrument, granularity, count=10)
    if len(candles) < 6:
        return None
    closes = [float(c["mid"]["c"]) for c in candles]
    ma5 = sum(closes[-5:]) / 5
    latest_close = closes[-1]
    return "above" if latest_close > ma5 else "below"


def _ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = (price - ema) * multiplier + ema
    return ema


def get_ema_trend_position(instrument: str, granularity: str, fast: int = 10, slow: int = 20) -> Optional[str]:
    candles = get_candles(instrument, granularity, count=slow + 10)
    if len(candles) < slow + 1:
        return None
    closes = [float(c["mid"]["c"]) for c in candles]
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    if ema_fast is None or ema_slow is None:
        return None
    return "above" if ema_fast > ema_slow else "below"


def get_trend_position(instrument: str, granularity: str) -> Optional[str]:
    if ENABLE_EMA_TREND:
        return get_ema_trend_position(instrument, granularity)
    return get_ma5_position(instrument, granularity)

def check_ma5_alignment(instrument: str, require_aligned: int = 4) -> Optional[str]:
    """
    Check MA5 alignment across H4, H1, M30, M15 timeframes.
    Returns "BUY" if price above MA5 on enough timeframes,
    Returns "SELL" if price below MA5 on enough timeframes,
    Returns None if mixed.
    
    Args:
        instrument: Currency pair to check
        require_aligned: Minimum number of timeframes that must agree (3 or 4)
    """
    timeframes = SIGNAL_TIMEFRAMES  
    directions = []

    for tf in timeframes:
        try:
            candles = get_candles(instrument, tf, count=10)
            if len(candles) < 6:
                print(f"    {tf}: Not enough data → skip")
                return None

            ma5 = _ema([float(c["mid"]["c"]) for c in candles], period=5)
            current_price = float(candles[-1]["mid"]["c"])

            if current_price > ma5:
                directions.append("BUY")
                print(f"    {tf}: ABOVE MA5")
            else:
                directions.append("SELL")
                print(f"    {tf}: BELOW MA5")
        except Exception as e:
            print(f"    {tf}: Check failed: {e} → skip")
            return None

    buy_count = directions.count("BUY")
    sell_count = directions.count("SELL")

    if buy_count >= require_aligned:
        return "BUY"
    elif sell_count >= require_aligned:
        return "SELL"
    else:
        print(f"    → Mixed alignment: {buy_count}x BUY, {sell_count}x SELL (need ≥{require_aligned} same)")
        return None


def get_previous_day_low(instrument: str) -> Optional[float]:
    candles = get_candles(instrument, "D", count=3)
    if not candles:
        return None
    return float(candles[-1]["mid"]["l"])


def get_previous_day_high(instrument: str) -> Optional[float]:
    candles = get_candles(instrument, "D", count=3)
    if not candles:
        return None
    return float(candles[-1]["mid"]["h"])


def confirmed_breakout(instrument: str, level: float, direction: str, closes_required: int = BREAKOUT_CONFIRMATION_CLOSES) -> bool:
    candles = get_candles(instrument, "D", count=closes_required + 2)
    if len(candles) < closes_required:
        return False
    recent_closes = [float(c["mid"]["c"]) for c in candles[-closes_required:]]
    if direction == "above":
        return all(c > level for c in recent_closes)
    return all(c < level for c in recent_closes)


def get_live_prices(instrument: str) -> Optional[Dict[str, float]]:
    from utils import oanda_client
    try:
        pricing_module = __import__(
            "oandapyV20.endpoints.pricing", fromlist=["PricingInfo"]
        )
        req = pricing_module.PricingInfo(
            accountID=OANDA_ACCOUNT_ID,
            params={"instruments": instrument}
        )
        oanda_client.request(req)
        prices = req.response["prices"][0]
        return {
            "ask": float(prices["asks"][0]["price"]),
            "bid": float(prices["bids"][0]["price"])
        }
    except Exception as e:
        print(f"    Price fetch failed for {instrument}: {e}")
        return None

# ==========================================
# NEWS FILTER CLASS
# ==========================================


class NewsFilter:
    IMPACT_THRESHOLD = 2
    LOOKAHEAD_HOURS = 24
    MINUTES_BEFORE = 30
    MINUTES_AFTER = 60
    CACHE_TTL_SECONDS = 4 * 3600
    QUOTA_BACKOFF_SECONDS = 6 * 3600

    def __init__(self):
        self._cache: List[dict] | None = None
        self._cache_time: float | None = None
        self._failed = False
        self._quota_backoff_until: float | None = None
        self._client = None

    def reset_cycle(self) -> None:
        self._failed = False

    def degraded(self) -> bool:
        return self._failed

    def _in_quota_backoff(self) -> bool:
        return (self._quota_backoff_until is not None
                and time.time() < self._quota_backoff_until)

    def _get_client(self):
        if self._client is None:
            if api_key := os.getenv("GEMINI_API_KEY"):
                self._client = genai.Client(api_key=api_key)
            else:
                raise RuntimeError("GEMINI_API_KEY not found in environment (.env)")
            self._client = genai.Client(api_key=api_key)
        return self._client

    @staticmethod
    def _is_quota_error(exc: Exception) -> bool:
        text = str(exc).upper()
        return "RESOURCE_EXHAUSTED" in text or "429" in text or "QUOTA" in text

    def _call_gemini(self, model: str, prompt: str):
        client = self._get_client()
        return client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )

    @staticmethod
    def _parse_response(raw_text: str) -> List[dict]:
        if raw_text is None:
            raise ValueError("Gemini returned an empty response")
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            raise ValueError("Gemini response was not a JSON array")
        return parsed

    def _log_and_print_events(self, events: List[dict]) -> None:
        relevant = [e for e in events if e.get("impact", 0) >= self.IMPACT_THRESHOLD]
        if not relevant:
            return

        now = time.time()
        print(f"  [NEWS] Gemini: {len(relevant)} high-impact event(s) in next {self.LOOKAHEAD_HOURS}h")

        log_lines = []
        for event in relevant:
            time_str = event.get("time_utc")
            timing = "unknown timing"
            if time_str:
                with contextlib.suppress(Exception):
                    event_dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                    minutes_until = (event_dt.timestamp() - now) / 60.0
                    timing = (f"in {minutes_until:.0f} min" if minutes_until >= 0
                              else f"{abs(minutes_until):.0f} min ago")

            line = (f"    {event.get('currency', '?')} | impact={event.get('impact')} | "
                    f"{event.get('event', 'event')} | {time_str or 'unknown time'} | {timing}")
            print(line)
            log_lines.append(line)

        try:
            with open(NEWS_LOG_PATH, "a") as f:
                f.write(f"--- Checked at {datetime.now(timezone.utc).isoformat()} "
                        f"(source: Gemini grounding) ---\n")
                for line in log_lines:
                    f.write(line + "\n")
        except Exception as e:
            print(f"  [NEWS] Failed to write news log ({NEWS_LOG_PATH}): {e}")

    def _fetch_events(self) -> List[dict]:
        now = time.time()
        if (self._cache is not None and self._cache_time is not None and
                (now - self._cache_time) < self.CACHE_TTL_SECONDS):
            return self._cache

        if self._in_quota_backoff():
            remaining_min = (self._quota_backoff_until - now) / 60.0
            print(f"  [NEWS] Skipping Gemini call -- in quota backoff for {remaining_min:.0f} more min.")
            self._failed = True
            return self._cache or []

        prompt = (
            f"Search for high-impact economic or central-bank events for these "
            f"currencies: {', '.join(NEWS_CURRENCIES)}, scheduled within the next "
            f"{self.LOOKAHEAD_HOURS} hours from now. Current UTC time is "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}.\n\n"
            "Only include events widely considered HIGH impact for that currency "
            "(e.g. central bank rate decisions, NFP, CPI, GDP, flagship PMI releases) "
            "-- skip minor or routine releases.\n\n"
            "Respond with ONLY a JSON array, no markdown fences, no other text. "
            "Each item must have exactly these fields:\n"
            '{"currency": "USD", "event": "short event name", "impact": 1-3, '
            '"time_utc": "YYYY-MM-DDTHH:MM:SSZ"}\n'
            "If you are not confident of the exact time, give your best estimate "
            "rather than omitting the event. If there are no such events, return []."
        )

        events: List[dict] = []
        primary_quota_error = False

        try:
            response = self._call_gemini(GEMINI_NEWS_MODEL, prompt)
            events = self._parse_response(response.text)
            self._failed = False
        except Exception as e:
            primary_quota_error = self._is_quota_error(e)
            print(f"  [NEWS] Gemini ({GEMINI_NEWS_MODEL}) fetch failed: {str(e)[:150]}")
            self._failed = True

            if primary_quota_error:
                try:
                    print(f"  [NEWS] Quota error on primary model -- trying fallback {GEMINI_NEWS_FALLBACK_MODEL}...")
                    response = self._call_gemini(GEMINI_NEWS_FALLBACK_MODEL, prompt)
                    events = self._parse_response(response.text)
                    self._failed = False
                except Exception as e2:
                    fallback_quota_error = self._is_quota_error(e2)
                    print(f"  [NEWS] Fallback ({GEMINI_NEWS_FALLBACK_MODEL}) also failed: {str(e2)[:150]}")
                    if primary_quota_error and fallback_quota_error:
                        self._quota_backoff_until = now + self.QUOTA_BACKOFF_SECONDS
                        print(f"  [NEWS] Both models quota-exhausted -- backing off for {self.QUOTA_BACKOFF_SECONDS / 3600:.1f}h.")

        self._cache = events
        self._cache_time = now
        self._log_and_print_events(events)
        return events

    def should_avoid_pair(self, pair: str) -> Tuple[bool, str]:
        if not ENABLE_NEWS_FILTER:
            return False, ""

        events = self._fetch_events()
        if not events:
            return False, ""

        parts = pair.split("_")
        if len(parts) != 2:
            return False, ""
        relevant_currencies = set(parts)
        now = time.time()

        for event in events:
            currency = event.get("currency", "")
            if currency not in relevant_currencies:
                continue

            impact = event.get("impact", 0)
            if impact < self.IMPACT_THRESHOLD:
                continue

            time_str = event.get("time_utc")
            if not time_str:
                continue
            try:
                event_dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                event_ts = event_dt.timestamp()
            except Exception:
                continue

            minutes_until = (event_ts - now) / 60.0

            if 0 <= minutes_until <= self.MINUTES_BEFORE:
                return True, (f"{currency} '{event.get('event', 'event')}' "
                              f"(impact {impact}) in {int(minutes_until)} min [Gemini]")
            if -self.MINUTES_AFTER <= minutes_until < 0:
                return True, (f"{currency} '{event.get('event', 'event')}' "
                              f"(impact {impact}) {int(abs(minutes_until))} min ago [Gemini]")

        return False, ""


def get_dynamic_sl_tp(pair: str, entry: float, atr: float, sentiment: str) -> dict:
    prompt = f"""
    Suggest optimal SL/TP for {pair} entry={entry:.3f}, ATR={atr:.3f}.
    Consider: normal volatility, recent range, and news risk.
    Return JSON: {{"sl": 145.20, "tp": 147.50, "rr": 2.2}}
    """
    try:
        res = gemini_client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return json.loads(res.text.strip("`json \n"))
    except:
        return {"sl": entry - 2 * atr, "tp": entry + 4 * atr, "rr": 2.0}
