#!/usr/bin/env bash
#
# jitter-run.sh — wrap any command with a random delay before running it.
#
# Usage:
#   jitter-run.sh [-m MAX_SECONDS] [-l LOGFILE] -- COMMAND [ARGS...]
#
# Examples:
#   jitter-run.sh -- /path/to/backup.sh
#   jitter-run.sh -m 600 -- /usr/bin/python3 /opt/scripts/sync.py
#   jitter-run.sh -m 300 -l /var/log/jitter-run.log -- /path/to/job.sh --flag
#
# Cron usage (no manual bash -c wrapping needed):
#   */15 * * * 1-5   /usr/local/bin/jitter-run.sh -m 300 -- /path/to/script.sh

set -euo pipefail

MAX_SECONDS=300   # default max jitter: 5 minutes
LOGFILE=""
BLOCK_WINDOWS=""  # e.g. "21:00-22:00,00:00-08:00" (UTC, inclusive ranges)
USE_FOREX_BLOCK_PRESET=false

usage() {
    cat >&2 <<EOF
Usage: $0 [-m MAX_SECONDS] [-l LOGFILE] [-b WINDOWS] [-B] -- COMMAND [ARGS...]

Options:
  -m SECONDS     Max random jitter before running (default: 300)
  -l FILE        Append all log output to FILE instead of stdout
  -b WINDOWS     Block execution during these UTC time windows.
                 Format: "HH:MM-HH:MM[,HH:MM-HH:MM...]"
                 Example: "21:00-22:00,00:00-08:00"
  -B             Built-in forex low-liquidity block preset (shorthand for
                 -b "21:00-22:00" + weekend skip). Covers the gap between
                 NY close (~21:00 UTC) and Sydney open (~22:00 UTC), plus
                 the entire weekend (Fri 21:00 → Sun 22:00 UTC).
  -h, --help     Show this help and exit

Cron usage:
  */15 * * * 1-5  /path/to/jitter-run.sh -m 300 -B -- /path/to/trade_bot.py
EOF
    exit 1
}

# --- parse options ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        -m)
            MAX_SECONDS="$2"
            shift 2
            ;;
        -l)
            LOGFILE="$2"
            shift 2
            ;;
        -b)
            BLOCK_WINDOWS="$2"
            shift 2
            ;;
        -B)
            USE_FOREX_BLOCK_PRESET=true
            shift
            ;;
        --)
            shift
            break
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            ;;
    esac
done

if [[ $# -eq 0 ]]; then
    echo "No command given." >&2
    usage
fi

if ! [[ "$MAX_SECONDS" =~ ^[0-9]+$ ]] || [[ "$MAX_SECONDS" -le 0 ]]; then
    echo "MAX_SECONDS must be a positive integer." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Block-window logic — skip execution entirely when current UTC time falls
# inside any configured "do not run" window. Designed for forex traders who
# want to avoid the low-liquidity dead zone between NY close and Sydney open.
# ---------------------------------------------------------------------------

_ts_log() {
    local msg="[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"
    if [[ -n "$LOGFILE" ]]; then
        echo "$msg" >> "$LOGFILE"
    else
        echo "$msg"
    fi
}

# Convert "HH:MM" → total minutes since midnight.
_to_minutes() {
    local t="$1"
    local h m
    h="${t%:*}"
    m="${t#*:}"
    echo $((10#$h * 60 + 10#$m))
}

# Check if current UTC time is inside a single "HH:MM-HH:MM" range.
# Supports wrap-around ranges like "22:00-02:00".
_in_range() {
    local current="$1" start="$2" end="$3"
    if [[ "$start" -le "$end" ]]; then
        # Normal range: e.g. 08:00-17:00
        [[ "$current" -ge "$start" && "$current" -lt "$end" ]]
    else
        # Wrap-around: e.g. 22:00-02:00
        [[ "$current" -ge "$start" || "$current" -lt "$end" ]]
    fi
}

# Expand -B preset into concrete block windows + weekend guard.
# Returns two things via global vars: FOREX_BLOCK_WINDOWS (time list) and
# FOREX_BLOCK_WEEKEND (boolean for "skip entire weekend").
_apply_forex_preset() {
    FOREX_BLOCK_WINDOWS="21:00-22:00"   # Mon–Thu only; weekend handled below
    FOREX_BLOCK_WEEKEND=true
}

# Merge -b and -B windows, dedupe, and store in MERGED_BLOCK_WINDOWS.
MERGED_BLOCK_WINDOWS=""
BLOCK_WEEKEND=false

if [[ "$USE_FOREX_BLOCK_PRESET" == true ]]; then
    _apply_forex_preset
    MERGED_BLOCK_WINDOWS="$FOREX_BLOCK_WINDOWS"
    BLOCK_WEEKEND="$FOREX_BLOCK_WEEKEND"
fi
if [[ -n "$BLOCK_WINDOWS" ]]; then
    if [[ -n "$MERGED_BLOCK_WINDOWS" ]]; then
        MERGED_BLOCK_WINDOWS="${MERGED_BLOCK_WINDOWS},${BLOCK_WINDOWS}"
    else
        MERGED_BLOCK_WINDOWS="$BLOCK_WINDOWS"
    fi
fi

# --- weekend skip (part of -B preset) ---
if [[ "$BLOCK_WEEKEND" == true ]]; then
    _dow=$(date -u '+%u')   # 1=Mon … 7=Sun
    _hm_now=$(date -u '+%H:%M')
    _now_min=$(_to_minutes "$_hm_now")

    # Fri 21:00 UTC → entire weekend skipped
    if [[ "$_dow" == "5" && "$_now_min" -ge $(_to_minutes "21:00") ]]; then
        _ts_log "BLOCKED (weekend preset): Fri after 21:00 UTC"
        exit 0
    fi
    # Sat → blocked all day
    if [[ "$_dow" == "6" ]]; then
        _ts_log "BLOCKED (weekend preset): Saturday all day"
        exit 0
    fi
    # Sun before 22:00 UTC → blocked (Sydney hasn't opened yet)
    if [[ "$_dow" == "7" && "$_now_min" -lt $(_to_minutes "22:00") ]]; then
        _ts_log "BLOCKED (weekend preset): Sun before 22:00 UTC"
        exit 0
    fi
fi

# --- per-time-window skip ---
if [[ -n "$MERGED_BLOCK_WINDOWS" ]]; then
    _hm_now=$(date -u '+%H:%M')
    _now_min=$(_to_minutes "$_hm_now")

    IFS=',' read -ra _windows <<< "$MERGED_BLOCK_WINDOWS"
    for _win in "${_windows[@]}"; do
        _start="${_win%%-*}"
        _end="${_win#*-}"
        _start_min=$(_to_minutes "$_start")
        _end_min=$(_to_minutes "$_end")
        if _in_range "$_now_min" "$_start_min" "$_end_min"; then
            _ts_log "BLOCKED (time window preset): ${_start}-${_end} UTC, current=${_hm_now} UTC"
            exit 0
        fi
    done
fi

# --- compute jitter ---
# Use /dev/urandom instead of $RANDOM for better portability across shells/cron envs
JITTER=$(( $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % (MAX_SECONDS + 1) ))

TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
MSG="[$TIMESTAMP] Sleeping ${JITTER}s (max ${MAX_SECONDS}s) before: $*"

if [[ -n "$LOGFILE" ]]; then
    echo "$MSG" >> "$LOGFILE"
else
    echo "$MSG"
fi

sleep "$JITTER"

# --- run the actual command, preserving its exit code ---
if [[ -n "$LOGFILE" ]]; then
    "$@" >> "$LOGFILE" 2>&1
else
    "$@"
fi
EXIT_CODE=$?

DONE_MSG="[$(date '+%Y-%m-%d %H:%M:%S')] Finished (exit ${EXIT_CODE}): $*"
if [[ -n "$LOGFILE" ]]; then
    echo "$DONE_MSG" >> "$LOGFILE"
else
    echo "$DONE_MSG"
fi

exit "$EXIT_CODE"