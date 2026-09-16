#!/usr/bin/env python3
"""FX Monte Carlo CLI Client (Daily & Weekly)."""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date

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
    timeframe: str, date_val: str, local_dir: str, debug: bool = False
) -> str | None:
    tf = timeframe.upper()
    if not os.path.isdir(local_dir):
        if debug:
            print(f"[DEBUG] local_dir not found: {local_dir}", file=sys.stderr)
        return None

    files = os.listdir(local_dir)
    pattern = re.compile(rf"^mc_{tf}_all_pairs_(\d{{8}})_(\d{{4}})\.json$")

    candidates = []
    for f in files:
        m = pattern.match(f)
        if m:
            candidates.append((m.group(1), m.group(2), f))

    if not candidates:
        if debug:
            print(f"[DEBUG] no local {tf} files", file=sys.stderr)
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


def _normalize_local_result(data: dict, pair: str | None) -> dict:
    if "pairs" in data:
        return data

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
    url = _build_url(timeframe, date_val, pair, base_url)
    data = fetch_json_soft(url, debug=debug)
    if data is not None:
        return _normalize_pair_object(data)

    local_path = _local_find_file(timeframe, date_val, local_dir, debug=debug)
    if local_path:
        if debug:
            print(f"[DEBUG] falling back to local: {local_path}", file=sys.stderr)
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
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()