from __future__ import annotations

import re
import unittest
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[2]


class ContainerPackagingContractTest(unittest.TestCase):
    def test_image_builds_the_frontend_then_installs_the_locked_runtime(self) -> None:
        dockerfile = (PRODUCT_ROOT / "Dockerfile").read_text()

        self.assertRegex(dockerfile, r"(?m)^FROM node:22[^ ]* AS frontend-build$")
        self.assertIn("RUN npm ci", dockerfile)
        self.assertIn("RUN npm run build", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^FROM python:3\.12[^ ]* AS runtime$")
        self.assertIn("backend/requirements.lock", dockerfile)
        self.assertRegex(dockerfile, r"RUN /opt/venv/bin/python -m pip install")
        self.assertIn("--requirement /app/backend/requirements.lock", dockerfile)
        self.assertIn("--from=frontend-build", dockerfile)
        self.assertIn("/build/frontend/dist /app/frontend/dist", dockerfile)

    def test_runtime_is_non_root_and_uses_the_hardened_hosted_entrypoint(self) -> None:
        dockerfile = (PRODUCT_ROOT / "Dockerfile").read_text()

        self.assertIn("USER commerceops", dockerfile)
        self.assertIn('CMD ["bash", "scripts/start-hosted.sh"]', dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertNotRegex(dockerfile, re.compile(r"(?i)(password|secret|token)=\S+"))

    def test_runtime_image_records_the_exact_source_revision(self) -> None:
        dockerfile = (PRODUCT_ROOT / "Dockerfile").read_text()

        self.assertRegex(dockerfile, r"(?m)^ARG SOURCE_SHA$")
        self.assertIn(
            'LABEL org.opencontainers.image.revision="$SOURCE_SHA"', dockerfile
        )
        self.assertIn('RUN test -n "$SOURCE_SHA"', dockerfile)

    def test_healthcheck_uses_the_runtime_port_and_database_readiness(self) -> None:
        dockerfile = (PRODUCT_ROOT / "Dockerfile").read_text()

        self.assertIn("os.environ['PORT']", dockerfile)
        self.assertIn("'/ready'", dockerfile)
        self.assertNotIn("127.0.0.1:8000/health", dockerfile)

    def test_container_defaults_keep_demo_state_in_the_mounted_data_directory(
        self,
    ) -> None:
        dockerfile = (PRODUCT_ROOT / "Dockerfile").read_text()

        for setting in (
            "COMMERCE_OPS_DATABASE_URL=sqlite+pysqlite:////app/data/commerce_ops.db",
            "COMMERCE_OPS_ENVIRONMENT=demo",
            "COMMERCE_OPS_VENV_DIR=/opt/venv",
            "PORT=8000",
        ):
            self.assertIn(setting, dockerfile)
        self.assertIn('VOLUME ["/app/data"]', dockerfile)

    def test_build_context_excludes_local_state_dependencies_and_test_artifacts(
        self,
    ) -> None:
        dockerignore = (PRODUCT_ROOT / ".dockerignore").read_text().splitlines()

        for entry in (
            ".git",
            ".venv",
            "data",
            "frontend/node_modules",
            "frontend/dist",
            "frontend/playwright-report",
            "frontend/test-results",
            "backend/postgres_tests",
            "backend/tests",
            "scripts/tests",
        ):
            self.assertIn(entry, dockerignore)


if __name__ == "__main__":
    unittest.main()
