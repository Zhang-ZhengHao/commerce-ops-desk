from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[2]


class ProjectToolingContractTest(unittest.TestCase):
    def test_makefile_exposes_the_complete_i03_verification_surface(self) -> None:
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
            f"missing I03 targets: {targets}",
        )
        self.assertIn("complete I03 verification gate", makefile.read_text())

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

    def test_ci_uses_full_history_least_privilege_and_every_i03_gate(self) -> None:
        workflow = PRODUCT_ROOT / ".github" / "workflows" / "verify.yml"
        self.assertTrue(workflow.is_file(), "verify workflow must exist")
        contents = workflow.read_text()

        self.assertRegex(contents, r"(?m)^permissions:\n  contents: read$")
        self.assertIn("fetch-depth: 0", contents)
        self.assertIn(
            "ref: ${{ github.event.pull_request.head.sha || github.sha }}", contents
        )
        self.assertIn("i03-order-case-workflow:", contents)
        self.assertIn("name: Order and case workflow", contents)
        for command in (
            "make setup",
            "make backend-test",
            "make project-test",
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

    def test_makefile_exposes_the_live_postgresql_migration_gate(self) -> None:
        contents = (PRODUCT_ROOT / "Makefile").read_text()

        self.assertRegex(contents, r"(?m)^postgres-migration-test:$")
        self.assertIn(
            "pytest postgres_tests/test_harness.py postgres_tests/test_migrations.py",
            contents,
        )
        self.assertIn("mypy app alembic tests postgres_tests", contents)

    def test_makefile_exposes_the_live_postgresql_runtime_and_combined_gates(
        self,
    ) -> None:
        contents = (PRODUCT_ROOT / "Makefile").read_text()

        self.assertRegex(contents, r"(?m)^postgres-integration-test:$")
        self.assertIn(
            "pytest postgres_tests/test_workflow.py postgres_tests/test_concurrency.py",
            contents,
        )
        self.assertRegex(
            contents,
            r"(?m)^postgres-test: postgres-migration-test postgres-integration-test$",
        )

    def test_makefile_exposes_the_hardened_postgresql_container_gate(self) -> None:
        makefile = (PRODUCT_ROOT / "Makefile").read_text()
        pyproject = (PRODUCT_ROOT / "backend" / "pyproject.toml").read_text()

        self.assertRegex(makefile, r"(?m)^postgres-container-test:$")
        self.assertIn(
            "pytest -m container postgres_tests/test_container.py",
            makefile,
        )
        self.assertIn(
            '"container: requires Docker and a live PostgreSQL service"', pyproject
        )

    def test_ci_preserves_browser_failure_evidence(self) -> None:
        contents = (PRODUCT_ROOT / ".github" / "workflows" / "verify.yml").read_text()

        self.assertIn("actions/upload-artifact@v4", contents)
        self.assertIn("if: failure()", contents)
        self.assertIn("frontend/playwright-report/", contents)
        self.assertIn("frontend/test-results/", contents)

    def test_ci_runs_the_live_postgresql_and_production_container_gates(self) -> None:
        contents = (PRODUCT_ROOT / ".github" / "workflows" / "verify.yml").read_text()
        job_marker = "  postgres-integration:\n"

        self.assertIn(job_marker, contents)
        job = contents.split(job_marker, maxsplit=1)[1]
        self.assertIn("name: PostgreSQL 17 and production container", job)
        self.assertIn("timeout-minutes: 30", job)
        self.assertIn("permissions:\n      contents: read", job)
        self.assertIn(
            "image: postgres:17-alpine@sha256:"
            "b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24",
            job,
        )
        self.assertIn("ports:\n          - 5432/tcp", job)
        self.assertIn("pg_isready -U commerce_ops_ci -d postgres", job)
        self.assertIn("--health-start-period 10s", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn('COMMERCE_OPS_SETUP_FRONTEND: "0"', job)
        self.assertIn('COMMERCE_OPS_SETUP_BROWSER: "0"', job)
        self.assertIn(
            "COMMERCE_OPS_CONTAINER_IMAGE: commerce-ops-desk:postgres-ci",
            job,
        )

        dynamic_admin_url = (
            "postgresql+psycopg://commerce_ops_ci:ci_only_postgres@127.0.0.1:"
            "${{ job.services.postgres.ports[5432] }}/postgres"
        )
        self.assertEqual(job.count(dynamic_admin_url), 2)
        commands = (
            "run: make postgres-test",
            'run: docker build --file Dockerfile --tag "$COMMERCE_OPS_CONTAINER_IMAGE" .',
            "run: make postgres-container-test",
        )
        command_offsets = [job.index(command) for command in commands]
        self.assertEqual(command_offsets, sorted(command_offsets))

        for forbidden in (
            "secrets.",
            "actions/upload-artifact",
            "docker login",
            "docker push",
        ):
            self.assertNotIn(forbidden, job)

    def test_public_docs_separate_verified_i03_scope_from_the_roadmap(
        self,
    ) -> None:
        readme = (PRODUCT_ROOT / "README.md").read_text()
        design_summary = (PRODUCT_ROOT / "docs" / "design-summary.md").read_text()
        security_model_path = PRODUCT_ROOT / "docs" / "security-model.md"
        self.assertTrue(security_model_path.is_file())
        security_model = security_model_path.read_text()

        for heading in (
            "## What you can verify",
            "### 90-second walkthrough",
            "## Architecture",
            "## Run locally",
            "## Verified scope and limits",
        ):
            self.assertIn(heading, readme)
        self.assertIn("I03 order/case vertical slice", readme)
        self.assertIn("docs/assets/demo-entry-i03.png", readme)
        self.assertIn("docs/assets/exception-workflow.png", readme)
        self.assertIn("Signed webhook intake, transactional outbox processing", readme)
        self.assertIn("remain roadmap work", readme)

        self.assertIn("## Current verified slice", design_summary)
        self.assertIn(
            "I03 delivers the first complete order-exception workflow",
            design_summary,
        )
        for heading in (
            "## Product outcome",
            "## Users and authorization",
            "## System boundaries",
            "## Reliability model",
            "## Security and privacy boundaries",
            "## Verification evidence",
        ):
            self.assertIn(heading, design_summary)
        self.assertIn("Signed webhook intake, the outbox worker", design_summary)
        self.assertIn("remain planned", design_summary)

        for heading in (
            "## Threat model",
            "## Session and write controls",
            "## Tenant and role boundaries",
            "## Transaction and idempotency boundaries",
            "## Abuse and resource controls",
            "## Data and deployment limits",
        ):
            self.assertIn(heading, security_model)
        self.assertIn(
            "Production fails closed without an explicit secret and database URL",
            security_model,
        )
        self.assertIn("The public demo is single-node", security_model)
        self.assertIn(
            "PostgreSQL models and Alembic DDL compile offline",
            security_model,
        )
        self.assertIn(
            "no live PostgreSQL migration, transaction, lock, or concurrency result is claimed",
            security_model,
        )

        for relative_path in (
            "docs/assets/demo-entry-i03.png",
            "docs/assets/exception-workflow.png",
        ):
            self.assertTrue((PRODUCT_ROOT / relative_path).is_file())


if __name__ == "__main__":
    unittest.main()
