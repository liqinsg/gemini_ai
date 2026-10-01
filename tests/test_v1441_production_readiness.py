"""Regression tests for the two v1441 production-readiness changes.

1. CLI now fails closed on unrecognized arguments.
   ``parse_known_args`` used to silently DROP them, so ``--profile1`` (missing
   space) ran the DEFAULT profile — a different OANDA account than requested.

2. The per-profile flock is wired back into ``__main__``.
   It had been commented out, so two overlapping runs of the same profile could
   both pass the idempotency gate and stack duplicate entries.

The subprocess tests deliberately run WITHOUT ``--live`` and with ``DRY_RUN=true``
so that, even if the CLI guard ever regresses, the worst case is a practice-mode
dry run — never a live order.

Run (all three work):
    python tests/test_v1441_production_readiness.py
    cd tests && python test_v1441_production_readiness.py
    python -m unittest -v tests.test_v1441_production_readiness
"""

import fcntl
import os
import subprocess
import sys
import unittest
from pathlib import Path

# Running this file as a script puts *this* directory on sys.path, not the
# project root, so the repo imports would fail (same convention as the other
# tests in this package).
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import scheduled_runner_v1441 as runner

# Never used by a real runner: keeps these tests away from /tmp/runner_1.lock etc.
TEST_PROFILE = 99
TEST_LOCK = Path(f"/tmp/runner_{TEST_PROFILE}.lock")


class TestCliFailsClosed(unittest.TestCase):
    def _run(self, *args):
        env = {**os.environ, "DRY_RUN": "true"}
        env.pop("OANDA_ENV", None)  # practice unless --live is passed
        return subprocess.run(
            [sys.executable, str(runner.__file__), *args],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def test_profile_without_space_is_rejected_with_a_hint(self):
        proc = self._run("--profile1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        output = proc.stdout + proc.stderr
        self.assertIn("unrecognized argument", output)
        self.assertIn("--profile1", output)
        self.assertIn("--profile 1", output)

    def test_unknown_flag_is_rejected(self):
        proc = self._run("--definitely-not-an-option")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("unrecognized argument", proc.stdout + proc.stderr)

    def test_help_still_works(self):
        """Control: the parser itself is intact and exits before the guard."""
        proc = self._run("--help")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--profile", proc.stdout)


class TestProfileLockWired(unittest.TestCase):
    def setUp(self):
        TEST_LOCK.unlink(missing_ok=True)

    def tearDown(self):
        TEST_LOCK.unlink(missing_ok=True)

    def test_lock_is_acquired_when_free(self):
        lock_file = runner._acquire_profile_lock(TEST_PROFILE)
        try:
            self.assertIsNotNone(lock_file)
            self.assertFalse(lock_file.closed)
        finally:
            lock_file.close()  # releases the flock

    def test_lock_blocks_a_second_holder(self):
        """flock on a second fd conflicts even inside one process."""
        holder = open(TEST_LOCK, "a+")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(SystemExit) as caught:
                runner._acquire_profile_lock(TEST_PROFILE)
            self.assertEqual(caught.exception.code, 0)  # clean skip, not an error
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

    def test_lock_is_released_after_close(self):
        first = runner._acquire_profile_lock(TEST_PROFILE)
        first.close()
        second = runner._acquire_profile_lock(TEST_PROFILE)
        second.close()

    def test_main_block_actually_acquires_the_lock(self):
        """Guard against the call being commented out again."""
        source = Path(runner.__file__).read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r"(?m)^\s{4}_lock_fd = _acquire_profile_lock\(_args\.profile\)",
        )
        self.assertNotRegex(source, r"(?m)^\s*#\s*_lock_fd = _acquire_profile_lock")


if __name__ == "__main__":
    unittest.main()
