import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_local_sync as runner


def git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True, stderr=subprocess.DEVNULL).strip()


class LocalSyncRecoveryTests(unittest.TestCase):
    def test_pending_commit_is_pushed_even_when_next_sync_has_no_new_tokens(self):
        original = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                remote, local = root / "remote.git", root / "local"
                git(root, "init", "--bare", str(remote))
                git(root, "clone", str(remote), str(local))
                git(local, "checkout", "-b", "main")
                git(local, "config", "user.name", "Test")
                git(local, "config", "user.email", "test@example.com")
                (local / "data").mkdir()
                (local / "data/model_usage.json").write_text('{"total_tokens": 1}\n')
                git(local, "add", ".")
                git(local, "commit", "-m", "Initial")
                git(local, "push", "origin", "main")
                (local / "data/model_usage.json").write_text('{"total_tokens": 2}\n')
                git(local, "add", ".")
                git(local, "commit", "-m", "Sync local model usage")
                pending = git(local, "rev-parse", "HEAD")
                real_command = runner.command
                def command(*args, **kwargs):
                    if "scripts/sync_model_usage.py" in args:
                        return ""  # No further model changes after a failed push.
                    return real_command(*args, **kwargs)
                with patch.object(runner, "command", side_effect=command):
                    runner.sync(local)
                    self.assertEqual(git(remote, "rev-parse", "main"), pending)
                    self.assertEqual(git(local, "status", "--porcelain"), "")
                    with patch.object(runner, "push_pending") as push:
                        runner.sync(local, scheduled=True)
                        push.assert_not_called()
        finally:
            os.chdir(original)

    def test_failed_dispatch_remains_pending_for_the_next_run(self):
        original = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                git_dir = root / ".git"
                git_dir.mkdir()
                def command(*args, **kwargs):
                    if args[:2] == ("git", "rev-parse"):
                        return str(git_dir)
                    if args[:2] == ("gh", "workflow"):
                        raise subprocess.CalledProcessError(1, args)
                    return ""
                with patch.object(runner, "command", side_effect=command), patch.object(runner, "push_pending"):
                    with self.assertRaises(subprocess.CalledProcessError):
                        runner.sync(root, refresh=True)
                state_path = git_dir / "velocity-sync-state.json"
                self.assertTrue(json.loads(state_path.read_text())["pending_refresh"])
                with patch.object(runner, "command", side_effect=lambda *a, **k: str(git_dir) if a[:2] == ("git", "rev-parse") else ""), \
                     patch.object(runner, "push_pending"):
                    runner.sync(root)
                self.assertFalse(json.loads(state_path.read_text())["pending_refresh"])
        finally:
            os.chdir(original)

    def test_launch_agent_is_daily_with_login_catchup(self):
        path = Path(__file__).resolve().parents[1] / "launchd/com.mileslow.github-line-velocity.plist"
        config = plistlib.loads(path.read_bytes())
        self.assertNotIn("StartInterval", config)
        self.assertEqual(config["StartCalendarInterval"], {"Hour": 8, "Minute": 17})
        self.assertTrue(config["RunAtLoad"])
        self.assertIn("--scheduled", config["ProgramArguments"])
