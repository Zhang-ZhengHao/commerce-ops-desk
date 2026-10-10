from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

import tomllib

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
RELEASE_VERSION = "0.2.1"


class ProjectToolingContractTest(unittest.TestCase):
    def test_release_docs_describe_the_live_evaluator_boundary_precisely(
        self,
    ) -> None:
        release_docs = {
            relative_path: (PRODUCT_ROOT / relative_path).read_text()
            for relative_path in (
                "README.md",
                "CHANGELOG.md",
                "docs/design-summary.md",
                "docs/security-model.md",
            )
        }
        required_statements = (
            "This checkpoint does not add or publish a live-demo CTA",
            (
                "does not claim that the evaluator is deployed, released, or "
                "approved for external access"
            ),
            (
                "The exact deployment hostname already exists in versioned "
                "engineering files and public Git history"
            ),
            (
                "The actual publication gates are enterprise access-code "
                "distribution and promotion of the deployment as a live evaluator"
            ),
            (
                "The enterprise access code must never be stored in or published "
                "through this repository"
            ),
            "Live evaluator: single-node SQLite; PostgreSQL 17: CI-verified path only",
        )
        false_url_absence_claim = re.compile(
            r"(?i)(?:does not publish|publishes no|contains no|provides no|has no)\s+"
            r"(?:a\s+)?live(?:[- ]demo| evaluator)?\s+URL"
        )

        for relative_path, contents in release_docs.items():
            with self.subTest(relative_path=relative_path):
                normalized = " ".join(contents.split())
                for statement in required_statements:
                    self.assertIn(statement, normalized)
                self.assertNotRegex(contents, false_url_absence_claim)

    def test_evaluator_design_and_plan_distinguish_hostname_from_live_demo_cta(
        self,
    ) -> None:
        design = (
            PRODUCT_ROOT
            / "docs"
            / "superpowers"
            / "specs"
            / "2026-10-10-evaluator-release-design.md"
        ).read_text()
        plan = (
            PRODUCT_ROOT
            / "docs"
            / "superpowers"
            / "plans"
            / "2026-10-10-evaluator-release-implementation-plan.md"
        ).read_text()

        design_status = re.search(r"(?m)^Status: (.+)$", design)
        plan_status = re.search(r"(?m)^Status: (.+)$", plan)
        self.assertIsNotNone(design_status)
        self.assertIsNotNone(plan_status)
        assert design_status is not None
        assert plan_status is not None
        self.assertEqual(design_status.group(1), plan_status.group(1))
        self.assertEqual(design_status.group(1), "approved for execution")

        self.assertNotIn("srrsh.aig.rest", design)
        self.assertNotRegex(design, r"(?m)^Target (?:demo|host):\s*`?https?://")
        design_normalized = " ".join(design.split())
        plan_normalized = " ".join(plan.split())
        for contents in (design_normalized, plan_normalized):
            self.assertIn(
                "The exact deployment hostname already exists in versioned "
                "engineering files and public Git history",
                contents,
            )
            self.assertIn(
                "This checkpoint does not add or promote a live-demo CTA",
                contents,
            )
            self.assertIn(
                "does not claim that the evaluator is deployed, released, or "
                "approved for external access",
                contents,
            )

        for stale_claim in (
            "not published at this checkpoint",
            "the repository has no live product entry point",
            "publish the URL only after",
            "Do not add a live URL",
        ):
            self.assertNotIn(stale_claim, design)
            self.assertNotIn(stale_claim, plan)

        for contents in (design_normalized, plan_normalized):
            self.assertIn("requests 1,000 records", contents)
            self.assertIn("accepts at most 999", contents)
            self.assertIn("1,000-result GitHub API cap", contents)
            self.assertNotIn("requests 1,001 records", contents)
            self.assertNotIn("accepts at most 1,000", contents)

        self.assertRegex(design, r"(?m)^Target host: .*not promoted as a live-demo CTA")
        for visible_control_label in (
            "Deliver new failure",
            "Replay same event",
            "Tamper after signing",
            "Send stale signature",
            "Assign to agent",
            "Update assignment",
            "Switch to Agent",
            "Internal note",
            "Add note",
            "Resolution reason",
            "Resolve case",
            "Event provenance",
            "Accountable timeline",
        ):
            with self.subTest(visible_control_label=visible_control_label):
                self.assertIn(f"`{visible_control_label}`", design)

    def test_release_version_is_synchronized_across_public_surfaces(self) -> None:
        backend_pyproject = tomllib.loads(
            (PRODUCT_ROOT / "backend" / "pyproject.toml").read_text()
        )
        frontend_package = json.loads(
            (PRODUCT_ROOT / "frontend" / "package.json").read_text()
        )
        frontend_lock = json.loads(
            (PRODUCT_ROOT / "frontend" / "package-lock.json").read_text()
        )
        app_main = (PRODUCT_ROOT / "backend" / "app" / "main.py").read_text()
        version_module = (PRODUCT_ROOT / "backend" / "app" / "version.py").read_text()
        app_version = re.search(r'(?m)^APP_VERSION: Final = "([^"]+)"$', version_module)
        self.assertIsNotNone(
            app_version, "the application version constant must be explicit"
        )
        assert app_version is not None
        self.assertIn("version=APP_VERSION", app_main)
        self.assertIn("from app.version import APP_VERSION", app_main)

        versions = {
            "backend package": backend_pyproject["project"]["version"],
            "application metadata": app_version.group(1),
            "frontend package": frontend_package["version"],
            "frontend lock root": frontend_lock["version"],
            "frontend lock package": frontend_lock["packages"][""]["version"],
        }
        for surface, version in versions.items():
            with self.subTest(surface=surface):
                self.assertEqual(version, RELEASE_VERSION)

        readme = (PRODUCT_ROOT / "README.md").read_text()
        container_tags = re.findall(r"commerce-ops-desk:(\d+\.\d+\.\d+)", readme)
        self.assertEqual(container_tags, [RELEASE_VERSION, RELEASE_VERSION])

    def test_makefile_exposes_the_main_verification_surface(self) -> None:
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
            f"missing main verification targets: {targets}",
        )
        self.assertIn("main SQLite/full-stack verification gate", makefile.read_text())

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

    def test_playwright_backend_has_retry_headroom_without_changing_production_limit(
        self,
    ) -> None:
        playwright_config = (
            PRODUCT_ROOT / "frontend" / "playwright.config.ts"
        ).read_text()
        application_config = (
            PRODUCT_ROOT / "backend" / "app" / "config.py"
        ).read_text()

        test_limit = re.search(
            r"(?m)^\s+COMMERCE_OPS_DEMO_SOURCE_HOURLY_LIMIT: '(\d+)',\s*$",
            playwright_config,
        )
        self.assertIsNotNone(
            test_limit,
            "the Playwright backend must set an explicit demo source limit",
        )
        assert test_limit is not None
        self.assertGreaterEqual(
            int(test_limit.group(1)),
            100,
            "the E2E source limit must cover both projects, CI retries, and headroom",
        )
        self.assertRegex(
            application_config,
            r"(?m)^\s+demo_source_hourly_limit: int = Field\(default=10, gt=0\)$",
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
            "pytest postgres_tests/test_workflow.py postgres_tests/test_concurrency.py "
            "postgres_tests/test_webhook_rate_limits.py "
            "postgres_tests/test_webhook_repository.py "
            "postgres_tests/test_webhook_processing.py "
            "postgres_tests/test_webhook_api.py",
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
        self.assertIn(
            "COMMERCE_OPS_SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}",
            job,
        )

        dynamic_admin_url = (
            "postgresql+psycopg://commerce_ops_ci:ci_only_postgres@127.0.0.1:"
            "${{ job.services.postgres.ports[5432] }}/postgres"
        )
        self.assertEqual(job.count(dynamic_admin_url), 2)
        commands = (
            "run: make postgres-test",
            (
                'run: docker build --build-arg "SOURCE_SHA=$COMMERCE_OPS_SOURCE_SHA" '
                '--file Dockerfile --tag "$COMMERCE_OPS_CONTAINER_IMAGE" .'
            ),
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

    def test_public_docs_separate_verified_i04_scope_from_the_roadmap(
        self,
    ) -> None:
        readme = (PRODUCT_ROOT / "README.md").read_text()
        design_summary = (PRODUCT_ROOT / "docs" / "design-summary.md").read_text()
        security_model_path = PRODUCT_ROOT / "docs" / "security-model.md"
        self.assertTrue(security_model_path.is_file())
        security_model = security_model_path.read_text()

        for heading in (
            "## What you can verify",
            "### Walkthrough",
            "## Architecture",
            "## Run locally",
            "## Verified scope and limits",
        ):
            self.assertIn(heading, readme)
        self.assertIn("I03 order/case vertical slice", readme)
        self.assertIn("I04 signed-webhook simulator slice", readme)
        self.assertIn("docs/assets/signed-webhook-workflow-v0.2.0.png", readme)
        self.assertIn("docs/assets/exception-workflow.png", readme)
        self.assertIn(
            "https://github.com/Zhang-ZhengHao/commerce-ops-desk/releases/tag/v0.2.0",
            readme,
        )
        self.assertIn("Signed machine ingress", readme)
        self.assertIn("Manager-only synthetic provider", readme)
        self.assertIn("server-signed envelope", readme)
        for scenario in ("fresh", "replay", "tamper", "stale"):
            self.assertIn(f"`{scenario}`", readme)
        self.assertIn("safe provenance", readme)
        self.assertIn('delivery uses `credentials: "omit"`', readme)
        self.assertIn("Recovery repeats only the GET reads", readme)
        self.assertIn("The signing route is absent in production", readme)
        self.assertIn(
            "https://github.com/Zhang-ZhengHao/commerce-ops-desk/issues/new?"
            "template=engineering-feedback.yml",
            readme,
        )
        self.assertIn(
            "PostgreSQL 17 migration, constraint, readiness, transaction",
            readme,
        )
        self.assertNotIn("A browser simulator, asynchronous outbox/worker", readme)
        walkthrough = readme.split("### Walkthrough", maxsplit=1)[1].split(
            "\n## ", maxsplit=1
        )[0]
        walkthrough_markers = (
            "synthetic provider panel",
            "`fresh`",
            "`replay`",
            "`tamper`",
            "`stale`",
            "Manager assigns",
            "Agent adds a note",
            "resolves the case",
        )
        walkthrough_offsets = [
            walkthrough.index(marker) for marker in walkthrough_markers
        ]
        self.assertEqual(walkthrough_offsets, sorted(walkthrough_offsets))

        for unsupported_claim in (
            "Stripe or Shopify adapter",
            "outbox/worker",
            "automatic delivery retry",
            "dead-letter queue (DLQ)",
            "exactly-once",
            "high availability",
            "production-ready",
            "performance claim",
        ):
            self.assertIn(unsupported_claim, readme)

        self.assertIn("## Current verified slice", design_summary)
        self.assertIn(
            "I03 delivers the first complete order-exception workflow",
            design_summary,
        )
        self.assertIn(
            "The I04 slice adds a Manager-only synthetic provider simulator",
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
        self.assertIn(
            "The complete signed envelope stays only in panel memory", design_summary
        )
        self.assertIn("committed delivery", design_summary)
        self.assertIn("GET-only recovery", design_summary)
        self.assertNotIn("No webhook-specific UI is added", design_summary)
        self.assertIn("An outbox worker", design_summary)
        self.assertIn("remain unimplemented", design_summary)

        for heading in (
            "## Threat model",
            "## Session and write controls",
            "## Signed webhook boundary",
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
        self.assertIn("The hosted synthetic demo is single-node", security_model)
        self.assertIn(
            "PostgreSQL 17 migrations, tenant constraints, readiness",
            security_model,
        )
        self.assertIn("does not claim exactly-once delivery", security_model)
        self.assertIn("The Manager-only signing route", security_model)
        self.assertIn("is not registered in production", security_model)
        self.assertIn(
            "never enters storage, URLs, logs, or error objects", security_model
        )
        self.assertIn("omits cookies and all browser credentials", security_model)
        self.assertIn("Safe case provenance", security_model)

        for relative_path in (
            "docs/assets/demo-entry-i03.png",
            "docs/assets/exception-workflow.png",
            "docs/assets/signed-webhook-workflow-v0.2.0.png",
            "docs/assets/social-preview-v0.2.0.png",
        ):
            self.assertTrue((PRODUCT_ROOT / relative_path).is_file())

    def test_engineering_feedback_issue_form_has_actionable_fields(self) -> None:
        form_path = (
            PRODUCT_ROOT / ".github" / "ISSUE_TEMPLATE" / "engineering-feedback.yml"
        )
        self.assertTrue(form_path.is_file(), "engineering feedback form must exist")
        form = form_path.read_text()

        for top_level_key in ("name", "description", "title", "labels", "body"):
            self.assertRegex(form, rf"(?m)^{top_level_key}:")

        required_fields = {
            "reproduction": "Reproduction steps",
            "environment": "Environment",
            "expected": "Expected behavior",
            "actual": "Actual behavior",
        }
        for field_id, label in required_fields.items():
            field = re.search(
                rf"(?ms)^  - type: [^\n]+\n    id: {field_id}\n.*?(?=^  - type:|\Z)",
                form,
            )
            self.assertIsNotNone(field, f"missing {field_id} field")
            assert field is not None
            self.assertIn(f"label: {label}", field.group())
            self.assertRegex(field.group(), r"(?m)^      required: true$")

        implementation_interest = re.search(
            r"(?ms)^  - type: [^\n]+\n    id: implementation_interest\n.*?"
            r"(?=^  - type:|\Z)",
            form,
        )
        self.assertIsNotNone(
            implementation_interest,
            "missing optional implementation interest field",
        )
        assert implementation_interest is not None
        self.assertIn(
            "label: Implementation interest (optional)",
            implementation_interest.group(),
        )
        self.assertRegex(
            implementation_interest.group(),
            r"(?m)^      required: false$",
        )


if __name__ == "__main__":
    unittest.main()
