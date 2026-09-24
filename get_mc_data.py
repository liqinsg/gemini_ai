#!/usr/bin/env python3
"""FX Monte Carlo CLI Client (Daily & Weekly)."""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime

DEFAULT_BASE_URL = "https://raw.githubusercontent.com/liqinsg/fx_monte_carlo/main/api"


def fetch_json(url: str, debug: bool = False):
    if debug:
        print(f"[DEBUG] Fetching: {url}", file=sys.stderr)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "FX-MonteCarlo-Client/1.0", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} Error for URL: {url}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Network/JSON Error: {e}", file=sys.stderr)
        sys.exit(1)


def _iso_week_to_date(s: str) -> str | None:
    m = re.fullmatch(r"(\d{4})-W(\d{2})", s)
    if not m:
        return None
    year, week = int(m.group(1)), int(m.group(2))
    try:
        d = date.fromisocalendar(year, week, 1)
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return None


def fetch_json_soft(url: str, debug: bool = False):
    if debug:
        print(f"[DEBUG] Fetching: {url}", file=sys.stderr)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "FX-MonteCarlo-Client/1.0", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        if debug:
            print(f"[DEBUG] Network fetch failed: {e}", file=sys.stderr)
        return None


def _build_url(timeframe: str, date_val: str, pair: str | None, base_url: str) -> str:
    tf = timeframe.upper()
    if tf == "W":
        path_prefix = "weekly/"
        if date_val == "latest":
            path_dir = "latest"
        else:
            resolved = _iso_week_to_date(date_val)
            path_dir = (resolved or date_val).replace("-", "/")
    else:
        path_prefix = ""
        path_dir = "latest" if date_val == "latest" else date_val.replace("-", "/")

    filename = f"{_normalize_pair_symbol(pair)}.json" if pair else "all.json"
    return f"{base_url}/{path_prefix}{path_dir}/{filename}"


def _local_find_file(
    timeframe: str, date_val: str, local_dir: str, tags: list[str], debug: bool = False
) -> str | None:
    # Search across all given tags (e.g. a specific pair AND "all_pairs") so a per-pair
    # cache can be reused, and so a same-day batch fetch also satisfies per-pair lookups.
    tf = timeframe.upper()
    if not os.path.isdir(local_dir):
        if debug:
            print(f"[DEBUG] local_dir not found: {local_dir}", file=sys.stderr)
        return None

    files = os.listdir(local_dir)
    tag_group = "|".join(re.escape(t) for t in tags)
    pattern = re.compile(rf"^mc_{tf}_({tag_group})_(\d{{8}})_(\d{{4}})\.json$")

    candidates = []
    for f in files:
        m = pattern.match(f)
        if m:
            candidates.append((m.group(2), m.group(3), f))

    if not candidates:
        if debug:
            print(f"[DEBUG] no local {tf} files for tags {tags}", file=sys.stderr)
        return None

    if date_val == "latest":
        candidates.sort(key=lambda x: (x[0], x[1]))
        chosen = candidates[-1][2]
        return os.path.join(local_dir, chosen)

    date_compact = date_val.replace("-", "")
    for d, t, f in candidates:
        if d == date_compact:
            return os.path.join(local_dir, f)

    if debug:
        print(
            f"[DEBUG] no local {tf} file for date {date_val}", file=sys.stderr
        )
    return None

def _local_is_today(local_path: str, timeframe: str = "D") -> bool:
    if not local_path:
        return False
    fname = os.path.basename(local_path)
    m = re.match(rf"^mc_{timeframe.upper()}_[A-Za-z0-9_]+_(\d{{8}})", fname)
    if not m:
        return False
    file_date = m.group(1)
    today_utc = datetime.utcnow().strftime("%Y%m%d")
    return file_date == today_utc

def _save_to_local(data: dict, timeframe: str, local_dir: str, pair: str | None = None) -> str | None:
    if not data:
        return None
    os.makedirs(local_dir, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M")
    # Single-pair fetches must not use the "all_pairs" name — that name is reserved for the
    # full daily snapshot and _local_find_file()/_local_is_today() treat it as authoritative,
    # so writing a single pair there shadows the real all-pairs cache for every other pair.
    tag = _normalize_pair_symbol(pair) if pair else "all_pairs"
    fname = f"mc_{timeframe.upper()}_{tag}_{stamp}.json"
    fpath = os.path.join(local_dir, fname)
    try:
        with open(fpath, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return fpath
    except Exception:
        return None

def _normalize_pair_symbol(pair: str) -> str:
    return pair.upper().replace("=X", "").replace("=", "").replace("_", "")


def _normalize_pair_object(data: dict) -> dict:
    if not data:
        return {"date": "", "generated_utc": "", "count": 0, "pairs": []}
    if "pairs" in data:
        return data
    single_date = data.get("date", "")
    return {
        "date": single_date,
        "generated_utc": data.get("generated_utc", ""),
        "count": 1,
        "pairs": [data],
    }


def _parse_stamp(generated_utc: str) -> tuple[str, str]:
    s = generated_utc.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.utcnow()
    return dt.strftime("%Y%m%d"), dt.strftime("%H%M")


def _save_remote_to_local(data: dict, tf: str, local_dir: str = "mc_results") -> str | None:
    if "pairs" not in data or not data["pairs"]:
        return None

    first_pair = data["pairs"][0]
    results_map = {}
    for p in data["pairs"]:
        pair_symbol = p.get("pair", "")
        if pair_symbol:
            results_map[f"{pair_symbol}=X"] = p

    generated_utc = data.get("generated_utc", "") or first_pair.get("generated_utc", "")
    date_stamp, time_stamp = _parse_stamp(generated_utc)

    payload = {
        "metadata": {
            "timeframe": tf,
            "generated_utc": generated_utc,
            "total_pairs": len(results_map),
            "simulations": first_pair.get("simulations", 5000),
            "lookback": first_pair.get("lookback", 90),
            "forecast": first_pair.get("forecast", 5),
        },
        "results": results_map,
    }

    os.makedirs(local_dir, exist_ok=True)
    filename = f"mc_{tf}_all_pairs_{date_stamp}_{time_stamp}.json"
    out_path = os.path.join(local_dir, filename)
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"[save] → {out_path}", file=sys.stderr)
    return out_path


def _normalize_local_result(data: dict, pair: str | None) -> dict:
    if "pairs" in data:
        return data

    # Flat single-pair document (e.g. per-pair remote endpoint) has no "results"/"pairs" wrapper.
    if "pair" in data and "results" not in data:
        return _normalize_pair_object(data)

    results = data.get("results", {})
    meta = data.get("metadata", {})

    if pair:
        pair_key = _normalize_pair_symbol(pair)
        match = None
        for k, v in results.items():
            if _normalize_pair_symbol(k) == pair_key:
                match = v
                break
        if match:
            pair_date = match.get("date") or meta.get("generated_utc", "")[:10]
            return {
                "date": pair_date,
                "generated_utc": meta.get("generated_utc", ""),
                "count": 1,
                "pairs": [match],
            }
        return {"date": "", "generated_utc": "", "count": 0, "pairs": []}

    pairs_list = list(results.values())
    meta_date = meta.get("generated_utc", "")[:10]
    return {
        "date": meta_date,
        "generated_utc": meta.get("generated_utc", ""),
        "count": len(pairs_list),
        "pairs": pairs_list,
    }


def get_mc_data(
    timeframe: str = "D",
    date_val: str = "latest",
    pair: str | None = None,
    local_dir: str = "mc_results",
    base_url: str = DEFAULT_BASE_URL,
    debug: bool = False,
) -> dict:


    tags = [_normalize_pair_symbol(pair), "all_pairs"] if pair else ["all_pairs"]
    local_path = _local_find_file(timeframe, date_val, local_dir, tags=tags, debug=debug)
    if local_path and _local_is_today(local_path, timeframe):
        if debug:
            print(f"[DEBUG] local today cache hit: {local_path}", file=sys.stderr)
        with open(local_path, "r") as f:
            local_data = json.load(f)
        return _normalize_local_result(local_data, pair)

    if local_path:
        if debug:
            print(
                f"[DEBUG] local cache exists but not today ({os.path.basename(local_path)}) → fetching fresh",
                file=sys.stderr,
            )
    else:
        if debug:
            print(f"[DEBUG] no local cache for {timeframe} → fetching", file=sys.stderr)

    url = _build_url(timeframe, date_val, pair, base_url)
    data = fetch_json_soft(url, debug=debug)
    if data is not None:
        _save_to_local(data, timeframe, local_dir, pair=pair)
        return _normalize_local_result(data, pair)

    if local_path:
        if debug:
            print(
                f"[DEBUG] network failed → falling back to stale local: {local_path}",
                file=sys.stderr,
            )
        with open(local_path, "r") as f:
            local_data = json.load(f)
        return _normalize_local_result(local_data, pair)

    raise RuntimeError(
        f"Failed to fetch {timeframe} {date_val} (pair={pair}): "
        f"network failed and no local file available"
    )


def main():
    parser = argparse.ArgumentParser(
        description="FX Monte Carlo Data Client (Daily & Weekly)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python get_mc_data.py --dates                      # List all available daily dates
  python get_mc_data.py --weeks                      # List all available weekly dates
  python get_mc_data.py --date latest                # Get latest daily snapshot (all pairs)
  python get_mc_data.py --date 2026-09-09            # Get daily snapshot for a specific day
  python get_mc_data.py --week latest                # Get latest weekly snapshot (all pairs)
  python get_mc_data.py --week 2026-09-10            # Get weekly snapshot for that date
        """,
    )

    parser.add_argument("--pair", type=str, help="Currency pair (e.g. EURUSD)")
    parser.add_argument("--date", type=str, help="Daily: YYYY-MM-DD or latest")
    parser.add_argument(
        "--week", type=str, help="Weekly: YYYY-MM-DD, YYYY-Www, or latest"
    )
    parser.add_argument(
        "--dates", action="store_true", help="List available daily dates"
    )
    parser.add_argument(
        "--weeks", action="store_true", help="List available weekly dates"
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print request URL to stderr"
    )

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    if args.dates:
        data = fetch_json(f"{DEFAULT_BASE_URL}/dates.json", debug=args.debug)
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    if args.weeks:
        data = fetch_json(f"{DEFAULT_BASE_URL}/weekly_dates.json", debug=args.debug)
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return

    if not args.date and not args.week:
        parser.error("one of --date, --week, --dates, or --weeks is required")

    if args.date and args.week:
        parser.error("--date and --week are mutually exclusive")

    if args.date:
        path_prefix = ""
        path_dir = args.date.replace("-", "/") if args.date != "latest" else "latest"
    else:
        path_prefix = "weekly/"
        if args.week == "latest":
            path_dir = "latest"
        else:
            resolved = _iso_week_to_date(args.week)
            if resolved:
                path_dir = resolved.replace("-", "/")
            else:
                path_dir = args.week.replace("-", "/")

    filename = f"{args.pair.upper()}.json" if args.pair else "all.json"
    target_url = f"{DEFAULT_BASE_URL}/{path_prefix}{path_dir}/{filename}"

    data = fetch_json(target_url, debug=args.debug)

    tf = "W" if args.week else "D"
    if not args.pair:
        _save_remote_to_local(data, tf=tf)

    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()