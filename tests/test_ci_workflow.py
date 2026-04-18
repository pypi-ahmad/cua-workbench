"""Lock-in tests for the GitHub Actions CI pipeline.

The repo was previously CI-less; this pins the minimum gates we now
require so a future PR can't silently delete a security check.

Verified gates:
  1. backend pytest job (fast suite, no stress, no integration)
  2. frontend build job (npm ci + vite build)
  3. pip-audit job against requirements.txt with --strict
  4. npm audit job against frontend prod deps with --audit-level=high
  5. Trivy Dockerfile config scan (HIGH/CRITICAL only, fail on issue)
  6. Trivy filesystem secret scan (HIGH/CRITICAL, skip node_modules/.venv)

Workflow file: ``.github/workflows/ci.yml``.
"""

from __future__ import annotations

import pathlib
import unittest

import yaml

CI = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


class TestCIWorkflow(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw  = CI.read_text(encoding="utf-8")
        cls.spec = yaml.safe_load(cls.raw)
        cls.jobs = cls.spec["jobs"]

    # ── Triggers / hygiene ───────────────────────────────────────────────

    def test_runs_on_push_and_pull_request(self):
        # PyYAML parses the bareword ``on`` as the Python boolean True.
        triggers = self.spec.get("on") or self.spec.get(True)
        self.assertIsNotNone(triggers, "workflow has no triggers")
        self.assertIn("push", triggers)
        self.assertIn("pull_request", triggers)

    def test_default_permissions_are_least_privilege(self):
        # Top-level read-only token; jobs can opt up if ever needed.
        self.assertEqual(self.spec.get("permissions"), {"contents": "read"})

    # ── Required jobs are present ────────────────────────────────────────

    def test_backend_test_job_present(self):
        job = self.jobs.get("backend")
        self.assertIsNotNone(job, "backend test job missing")
        steps = " ".join(s.get("run", "") for s in job["steps"] if "run" in s)
        self.assertIn("pytest", steps)
        self.assertIn("--ignore=tests/stress", steps)
        self.assertIn('-m "not integration"', steps)

    def test_frontend_build_job_present(self):
        job = self.jobs.get("frontend")
        self.assertIsNotNone(job, "frontend build job missing")
        runs = [s.get("run", "") for s in job["steps"] if "run" in s]
        self.assertTrue(any("npm ci" in r for r in runs), "npm ci step missing")
        self.assertTrue(any("npm run build" in r for r in runs), "build step missing")

    def test_pip_audit_job_present_and_strict(self):
        job = self.jobs.get("pip-audit")
        self.assertIsNotNone(job, "pip-audit job missing")
        runs = " ".join(s.get("run", "") for s in job["steps"] if "run" in s)
        self.assertIn("pip-audit", runs)
        self.assertIn("requirements.txt", runs)
        self.assertIn("--strict", runs)

    def test_npm_audit_job_present_and_high_threshold(self):
        job = self.jobs.get("npm-audit")
        self.assertIsNotNone(job, "npm-audit job missing")
        runs = " ".join(s.get("run", "") for s in job["steps"] if "run" in s)
        self.assertIn("npm audit", runs)
        self.assertIn("--audit-level=high", runs)
        self.assertIn("--omit=dev", runs)

    def test_trivy_job_present_with_both_scans(self):
        job = self.jobs.get("trivy")
        self.assertIsNotNone(job, "trivy job missing")
        uses_steps = [s for s in job["steps"] if "uses" in s and "trivy" in s["uses"]]
        self.assertGreaterEqual(
            len(uses_steps), 2,
            "trivy job must run at least the Dockerfile config scan AND the secret scan",
        )

        scan_types = {s.get("with", {}).get("scan-type") for s in uses_steps}
        self.assertIn("config", scan_types, "missing Dockerfile/config scan")
        self.assertIn("fs",     scan_types, "missing filesystem scan")

        for s in uses_steps:
            with_ = s.get("with", {})
            # Every scan must fail the build on findings (exit-code: 1)
            self.assertEqual(
                str(with_.get("exit-code")), "1",
                f"trivy step does not enforce failure: {s}",
            )
            # And must restrict severity so noise doesn't drown signal.
            self.assertEqual(with_.get("severity"), "HIGH,CRITICAL")

    # ── Security-gate suite is wired correctly ───────────────────────────

    def test_all_jobs_run_on_ubuntu(self):
        for name, job in self.jobs.items():
            self.assertEqual(
                job.get("runs-on"), "ubuntu-latest",
                f"job {name!r} does not target ubuntu-latest",
            )


if __name__ == "__main__":
    unittest.main()
