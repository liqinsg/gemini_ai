import os
import requests
from pathlib import Path
from typing import Optional, Dict, Any

GITHUB_REPO = "liqinsg/gemini_ai"
BRANCH = "main"
RESULTS_DIR_PATH = "daily_results"


def get_latest_mc(
    instrument: str,
    day: bool = True,
    repo: str = GITHUB_REPO,
    branch: str = BRANCH,
    token: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Fetches the latest Monte Carlo JSON result from GitHub for a given instrument and timeframe.

    :param instrument: Currency pair symbol (e.g., "EURUSD", "EURUSD=X", or "EUR_USD").
    :param day: True for Daily ('daily_mc'), False for H4 ('h4_mc'). Default is True.
    :param repo: GitHub repository in 'owner/repo' format.
    :param branch: Branch name (default 'main').
    :param token: Optional GitHub Personal Access Token (PAT). Reads from GITHUB_TOKEN env if not passed.
    :return: Parsed JSON dictionary or None if not found/error occurs.
    """
    # 1. Normalize instrument pair name (e.g., "EURUSD=X" -> "EURUSD")
    safe_pair = instrument.replace("=X", "").replace("=", "_").replace("_", "")
    
    # 2. Determine file prefix tag
    tag = "daily" if day else "h4"
    prefix = f"{tag}_mc_{safe_pair}_"

    # 3. Setup Headers (Authentication for private repos or higher API rate limits)
    headers = {"Accept": "application/vnd.github.v3+json"}
    auth_token = token or os.getenv("GITHUB_TOKEN")
    if auth_token:
        headers["Authorization"] = f"token {auth_token}"

    # 4. Fetch list of files in the directory via GitHub REST API
    api_url = f"https://api.github.com/repos/{repo}/contents/{RESULTS_DIR_PATH}?ref={branch}"
    
    try:
        res = requests.get(api_url, headers=headers, timeout=10)
        res.raise_for_status()
        files = res.json()
    except Exception as e:
        print(f"[MC LOADER ERROR] Failed to query GitHub API: {e}")
        return None

    # 5. Filter for matching files (e.g., "daily_mc_EURUSD_*.json")
    matching_files = [
        f for f in files
        if isinstance(f, dict) and f.get("name", "").startswith(prefix) and f.get("name", "").endswith(".json")
    ]

    if not matching_files:
        print(f"[MC LOADER] No matching MC JSON found for {instrument} (day={day}).")
        return None

    # 6. Sort chronologically by filename (since YYYYMMDD_HHMM format sorts alphabetically)
    latest_file_info = sorted(matching_files, key=lambda x: x["name"])[-1]
    download_url = latest_file_info.get("download_url")

    # 7. Fetch the actual raw JSON content
    try:
        raw_res = requests.get(download_url, headers=headers, timeout=10)
        raw_res.raise_for_status()
        return raw_res.json()
    except Exception as e:
        print(f"[MC LOADER ERROR] Failed to fetch raw JSON content from {download_url}: {e}")
        return None