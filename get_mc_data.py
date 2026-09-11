#!/usr/bin/env python3
"""FX Monte Carlo CLI Client (Daily & Weekly)."""

import argparse
import json
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
