from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[2]


class ProjectToolingContractTest(unittest.TestCase):
    def test_makefile_exposes_the_complete_i01_verification_surface(self) -> None:
        makefile = PRODUCT_ROOT / "Makefile"
        self.assertTrue(makefile.is_file(), "Makefile must exist")

        targets = set(
            re.findall(
                r"^([a-z][a-z0-9-]*):(?:\s|$)", makefile.read_text(), re.MULTILINE
            )
        )
        self.assertTrue(
            {
                "setup",
                "run",
                "backend-test",
                "frontend-test",
                "sqlite-migrate-test",
                "hosted-smoke",
                "e2e-smoke",
                "public-scan",
                "lint",
                "build",
                "verify",
            }.issubset(targets),
            f"missing I01 targets: {targets}",
        )

    def test_environment_example_is_runnable_without_containing_credentials(
        self,
    ) -> None:
        example = PRODUCT_ROOT / ".env.example"
        self.assertTrue(example.is_file(), ".env.example must exist")
        contents = example.read_text()

        self.assertRegex(contents, r"(?m)^COMMERCE_OPS_ENVIRONMENT=development$")
        self.assertRegex(
            contents,
            r"(?m)^COMMERCE_OPS_DATABASE_URL=sqlite\+pysqlite:///\./data/commerce_ops\.db$",
        )
        self.assertRegex(contents, r"(?m)^PORT=[0-9]+$")
        self.assertNotRegex(contents, r"(?i)(password|secret|token)\s*=\s*[^\s#]+")

    def test_ci_uses_full_history_least_privilege_and_every_i01_gate(self) -> None:
        workflow = PRODUCT_ROOT / ".github" / "workflows" / "verify.yml"
        self.assertTrue(workflow.is_file(), "verify workflow must exist")
        contents = workflow.read_text()

        self.assertRegex(contents, r"(?m)^permissions:\n  contents: read$")
        self.assertIn("fetch-depth: 0", contents)
        for command in (
            "make backend-test",
            "make frontend-test",
            "make sqlite-migrate-test",
            "make hosted-smoke",
            "make e2e-smoke",
            "make public-scan",
            "make lint",
            "make build",
        ):
            self.assertIn(command, contents)
        self.assertNotIn("secrets.", contents)

    def test_shell_entrypoints_are_syntax_valid(self) -> None:
        for relative_path in (
            "scripts/setup-local.sh",
            "scripts/start-local.sh",
            "scripts/hosted-smoke.sh",
        ):
            script = PRODUCT_ROOT / relative_path
            self.assertTrue(script.is_file(), f"{relative_path} must exist")
            result = subprocess.run(
                ["bash", "-n", str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_e2e_target_uses_the_local_runtime_created_by_setup(self) -> None:
        contents = (PRODUCT_ROOT / "Makefile").read_text()

        self.assertRegex(
            contents,
            r"(?m)^e2e-smoke:\n\tCOMMERCE_OPS_VENV_DIR=.*npm --prefix frontend run test:e2e$",
        )

    def test_hosted_smoke_builds_the_ignored_frontend_bundle_first(self) -> None:
        contents = (PRODUCT_ROOT / "Makefile").read_text()

        self.assertRegex(contents, r"(?m)^hosted-smoke: build$")
        self.assertRegex(contents, r"(?m)^project-test: build$")

    def test_ci_preserves_browser_failure_evidence(self) -> None:
        contents = (PRODUCT_ROOT / ".github" / "workflows" / "verify.yml").read_text()

        self.assertIn("actions/upload-artifact@v4", contents)
        self.assertIn("if: failure()", contents)
        self.assertIn("frontend/playwright-report/", contents)
        self.assertIn("frontend/test-results/", contents)

    def test_public_docs_separate_verified_scope_from_the_product_plan(self) -> None:
        readme = (PRODUCT_ROOT / "README.md").read_text()
        design_summary = (PRODUCT_ROOT / "docs" / "design-summary.md").read_text()

        self.assertIn("## Current verified scope", readme)
        self.assertIn("## Planned product workflow", readme)
        self.assertIn("not implemented yet", readme)
        self.assertIn("## Current verified slice", design_summary)
        self.assertIn("## Target product outcome (planned)", design_summary)
        self.assertIn("guardrail, not proof", design_summary)


if __name__ == "__main__":
    unittest.main()
