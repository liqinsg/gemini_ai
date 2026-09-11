"""Stable entry point for the current scheduled trading runner."""
from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(
        Path(__file__).with_name("scheduled_runner_v1.4.1.py"),
        run_name="__main__",
    )