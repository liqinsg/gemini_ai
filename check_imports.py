#!/usr/bin/env python3
"""
Quick import check script:
Scans all .py files in the project and reports any import errors.
"""

import os
import sys
import importlib.util
from typing import List, Tuple

# Add current directory to Python path so imports work correctly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def check_file(file_path: str) -> Tuple[bool, str]:
    """Try to import a single file and return status + message."""
    module_name = os.path.splitext(os.path.relpath(file_path).replace(os.sep, "."))[0]
    try:
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if not spec or not spec.loader:
            return False, "Could not load module spec"
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return True, "OK"
    except Exception as e:
        return False, f"FAILED: {type(e).__name__}: {str(e)}"

def scan_project(root_dir: str = ".") -> List[Tuple[str, bool, str]]:
    """Scan all .py files in the project directory."""
    results = []
    for dirpath, _, filenames in os.walk(root_dir):
        # Skip folders we don't want to check
        skip_folders = ("venv", "__pycache__", "logs", ".git", "tmp", "archive")
        if any(skip in dirpath for skip in skip_folders):
            continue
        for filename in filenames:
            if filename.endswith(".py"):
                full_path = os.path.join(dirpath, filename)
                status, msg = check_file(full_path)
                results.append((full_path, status, msg))
    return results

if __name__ == "__main__":
    print("=" * 70)
    print("📦 RUNNING IMPORT CHECK...")
    print("=" * 70)

    all_results = scan_project()
    passed = sum(1 for _, ok, _ in all_results if ok)
    failed = sum(1 for _, ok, _ in all_results if not ok)

    for path, ok, msg in all_results:
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"{status} | {path:<45} | {msg}")

    print("\n" + "=" * 70)
    print(f"📊 SUMMARY: Total = {len(all_results)} | Passed = {passed} | Failed = {failed}")
    print("=" * 70)

    if failed > 0:
        sys.exit(1)
    sys.exit(0)