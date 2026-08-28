"""
Regression test: CLUSTER_STATE_PATH must be absolute and anchored to
config.py's own directory, regardless of the process's current working
directory at invocation time.

Background / bug this guards against:
Prior to the fix, CLUSTER_STATE_PATH was a relative path
("state/open_clusters.json"). When scheduled_runner_v1.3.py was invoked
via cron, the process's CWD was the crontab user's default ($HOME =
/home/nie), not the project directory. This caused ClusterStateStore to
silently read/write /home/nie/state/open_clusters.json instead of
~/projects/gemini_api/state/open_clusters.json, making the real,
actively-managed cluster state invisible when inspecting the project's
own state/ directory.

This test asserts the fix holds under both scenarios:
  1. Import from the project directory as CWD.
  2. Import from an unrelated directory (simulating cron's $HOME CWD).
In both cases CLUSTER_STATE_PATH must resolve to the same absolute path.
"""

import os
import sys
import importlib
import unittest


class TestClusterStatePathIsCwdIndependent(unittest.TestCase):

    def setUp(self):
        # Directory containing config.py (the project root).
        self.project_root = os.path.dirname(os.path.abspath(__file__))
        if self.project_root not in sys.path:
            sys.path.insert(0, self.project_root)
        self._original_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._original_cwd)

    def _reload_config_from_cwd(self, cwd):
        os.chdir(cwd)
        import config
        importlib.reload(config)
        return config

    def test_path_is_absolute(self):
        config = self._reload_config_from_cwd(self.project_root)
        self.assertTrue(
            os.path.isabs(config.CLUSTER_STATE_PATH),
            "CLUSTER_STATE_PATH must be absolute so it is CWD-independent "
            "under cron (cron's default CWD is $HOME, not the project dir)."
        )

    def test_path_is_anchored_to_config_directory(self):
        config = self._reload_config_from_cwd(self.project_root)
        expected_dir = os.path.dirname(os.path.abspath(config.__file__))
        self.assertTrue(
            config.CLUSTER_STATE_PATH.startswith(expected_dir),
            "CLUSTER_STATE_PATH must be anchored to config.py's own "
            "directory, not derived from the process's CWD."
        )
        self.assertEqual(
            config.CLUSTER_STATE_PATH,
            os.path.join(expected_dir, "state", "open_clusters.json"),
        )

    def test_path_identical_regardless_of_invocation_cwd(self):
        # Simulate running from the project directory (e.g. manual testing).
        config_from_project = self._reload_config_from_cwd(self.project_root)
        path_from_project = config_from_project.CLUSTER_STATE_PATH

        # Simulate running from cron's default CWD ($HOME), or any
        # unrelated directory such as /tmp.
        home_dir = os.path.expanduser("~")
        unrelated_cwd = home_dir if os.path.isdir(home_dir) else "/tmp"
        config_from_home = self._reload_config_from_cwd(unrelated_cwd)
        path_from_home = config_from_home.CLUSTER_STATE_PATH

        self.assertEqual(
            path_from_project,
            path_from_home,
            "CLUSTER_STATE_PATH resolved differently depending on the "
            "process's CWD at import time — this is the exact bug that "
            "caused cron-run cycles to read/write a different state file "
            "than manual runs from the project directory."
        )

    def test_risk_integration_inherits_same_path(self):
        """
        utils/risk_integration.py falls back to a hardcoded relative
        default ('state/open_clusters.json') only if config.py lacks the
        attribute. Confirm it actually inherits config.py's absolute,
        anchored path rather than silently hitting that fallback.
        """
        config = self._reload_config_from_cwd(self.project_root)

        import utils.risk_integration as risk_integration
        importlib.reload(risk_integration)

        self.assertEqual(
            risk_integration.CLUSTER_STATE_PATH,
            config.CLUSTER_STATE_PATH,
            "risk_integration.CLUSTER_STATE_PATH diverged from "
            "config.CLUSTER_STATE_PATH — check the getattr(_config, "
            "'CLUSTER_STATE_PATH', ...) fallback is not being triggered."
        )
        self.assertTrue(os.path.isabs(risk_integration.CLUSTER_STATE_PATH))


if __name__ == "__main__":
    unittest.main()