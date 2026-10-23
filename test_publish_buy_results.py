"""Regression tests for concurrent GitHub buy/short publication."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from publish_buy_results import publish


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def scan(date, generated):
    return {"target_date": date, "generated_at_jst": generated,
            "session": "noon", "write_status": "accepted", "safety_gate_errors": []}


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.worker = root / "worker"
        self.other = root / "other"
        git(root, "init", "--bare", str(self.origin))
        git(root, "clone", str(self.origin), str(self.worker))
        git(root, "clone", str(self.origin), str(self.other))
        for repo in (self.worker, self.other):
            git(repo, "config", "user.name", "test")
            git(repo, "config", "user.email", "test@example.com")
        self.write_scan(self.worker, "2026-10-23T11:00:00+09:00")
        (self.worker / "data").mkdir(exist_ok=True)
        (self.worker / "data/universe_top1000.csv").write_text("code\n1000\n")
        git(self.worker, "add", ".")
        git(self.worker, "commit", "-m", "seed")
        git(self.worker, "push", "origin", "HEAD:main")
        git(self.other, "fetch", "origin", "main")
        git(self.other, "checkout", "-B", "main", "origin/main")

    def write_scan(self, repo, timestamp, status="accepted"):
        p = repo / "results/latest_noon.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        d = scan("2026-10-23", timestamp)
        d["write_status"] = status
        p.write_text(json.dumps(d))
        q = repo / "results/archive/2026-10-23_noon.json"
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(json.dumps(d))

    def invoke(self):
        old = Path.cwd()
        try:
            os.chdir(self.worker)
            return publish("noon")
        finally:
            os.chdir(old)

    def test_concurrent_short_commit_is_preserved(self):
        self.write_scan(self.worker, "2026-10-23T11:56:45+09:00")
        short = self.other / "results/latest_short_noon.json"
        short.write_text('{"target_date":"2026-10-23"}')
        git(self.other, "add", ".")
        git(self.other, "commit", "-m", "short scan")
        git(self.other, "push", "origin", "HEAD:main")
        self.assertEqual(self.invoke(), "saved")
        git(self.other, "pull", "--ff-only", "origin", "main")
        self.assertTrue(short.exists())
        self.assertEqual(json.loads((self.other / "results/latest_noon.json").read_text())["generated_at_jst"], "2026-10-23T11:56:45+09:00")

    def test_older_run_must_not_overwrite_newer_remote(self):
        self.write_scan(self.worker, "2026-10-23T11:30:00+09:00")
        self.write_scan(self.other, "2026-10-23T12:10:00+09:00")
        git(self.other, "add", ".")
        git(self.other, "commit", "-m", "newer")
        git(self.other, "push", "origin", "HEAD:main")
        self.assertEqual(self.invoke(), "superseded")

    def test_push_race_retries_without_rebase(self):
        import publish_buy_results as publisher
        self.write_scan(self.worker, "2026-10-23T11:56:45+09:00")
        real_git = publisher.git
        raced = False

        def inject_race(*args, **kwargs):
            nonlocal raced
            if args[:2] == ("push", "origin") and not raced:
                raced = True
                p = self.other / "results/latest_short_noon.json"
                p.write_text('{"target_date":"2026-10-23"}')
                git(self.other, "add", ".")
                git(self.other, "commit", "-m", "simulated concurrent short")
                git(self.other, "push", "origin", "HEAD:main")
            return real_git(*args, **kwargs)

        with patch.object(publisher, "git", side_effect=inject_race):
            self.assertEqual(self.invoke(), "saved")
        self.assertTrue(raced)
        git(self.other, "pull", "--ff-only", "origin", "main")
        self.assertTrue((self.other / "results/latest_short_noon.json").exists())

    def test_reject_failed_scan(self):
        self.write_scan(self.worker, "2026-10-23T12:10:00+09:00", status="rejected")
        with self.assertRaisesRegex(RuntimeError, "unverified"):
            self.invoke()


if __name__ == "__main__":
    unittest.main()
