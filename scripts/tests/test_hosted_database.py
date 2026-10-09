from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = PRODUCT_ROOT / "scripts" / "start-hosted.sh"
PLAYWRIGHT_CONFIG = PRODUCT_ROOT / "frontend" / "playwright.config.ts"
PLAYWRIGHT_DATABASE_LIFECYCLE = (
    PRODUCT_ROOT / "frontend" / "e2e" / "database-lifecycle.ts"
)
TEST_SESSION_SECRET = "hosted-database-test-secret-with-at-least-32-bytes"
DEFAULT_DATABASE_DIRECTORY = Path("/var/tmp/commerce-ops-desk")
DEFAULT_DATABASE_URL = "sqlite+pysqlite:////var/tmp/commerce-ops-desk/commerce_ops.db"


class IsolatedHostedDatabaseStart:
    """Capture the database URL reaching both hosted startup boundaries."""

    def __init__(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="commerce-ops-database-", dir="/var/tmp"
        )
        self.root = Path(self._temporary_directory.name)
        self.product_root = self.root / "product"
        self.script = self.product_root / "scripts" / "start-hosted.sh"
        self.venv_dir = self.root / "venv"
        self.alembic_capture = self.root / "alembic-database-url"
        self.uvicorn_capture = self.root / "uvicorn-database-url"

        self.script.parent.mkdir(parents=True)
        self.script.write_bytes(START_SCRIPT.read_bytes())
        self.script.chmod(0o700)
        (self.product_root / "frontend" / "dist").mkdir(parents=True)
        (self.product_root / "frontend" / "dist" / "index.html").write_text(
            "<!doctype html><title>database test</title>", encoding="utf-8"
        )
        (self.product_root / "backend").mkdir()
        (self.product_root / "backend" / "alembic.ini").write_text(
            "[alembic]\n", encoding="utf-8"
        )
        self._write_fake_python()

    def cleanup(self) -> None:
        self._temporary_directory.cleanup()

    def _write_fake_python(self) -> None:
        fake_python = self.venv_dir / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        real_python = shlex.quote(sys.executable)
        fake_python.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "-c" ]]; then
  exec {real_python} "$@"
fi

if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "alembic" ]]; then
  printf '%s' "${{COMMERCE_OPS_DATABASE_URL:-}}" > "$COMMERCE_OPS_TEST_ALEMBIC_CAPTURE"
  exit 0
fi

if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "uvicorn" ]]; then
  printf '%s' "${{COMMERCE_OPS_DATABASE_URL:-}}" > "$COMMERCE_OPS_TEST_UVICORN_CAPTURE"
  exit 0
fi

echo "unexpected fake python invocation" >&2
exit 97
""",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)

    def run(
        self,
        *,
        environment_name: str,
        database_url: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("COMMERCE_OPS_DATABASE_URL", None)
        environment.update(
            {
                "PORT": "43120",
                "COMMERCE_OPS_VENV_DIR": str(self.venv_dir),
                "COMMERCE_OPS_ENVIRONMENT": environment_name,
                "COMMERCE_OPS_SESSION_SECRET": TEST_SESSION_SECRET,
                "COMMERCE_OPS_TEST_ALEMBIC_CAPTURE": str(self.alembic_capture),
                "COMMERCE_OPS_TEST_UVICORN_CAPTURE": str(self.uvicorn_capture),
            }
        )
        if database_url is not None:
            environment["COMMERCE_OPS_DATABASE_URL"] = database_url
        return subprocess.run(
            ["bash", str(self.script)],
            cwd=self.product_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )


class HostedDatabaseContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hosted = IsolatedHostedDatabaseStart()

    def tearDown(self) -> None:
        self.hosted.cleanup()

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def test_demo_without_an_explicit_url_uses_private_host_local_sqlite(self) -> None:
        result = self.hosted.run(environment_name="demo")

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(self.hosted.alembic_capture.read_text(), DEFAULT_DATABASE_URL)
        self.assertEqual(self.hosted.uvicorn_capture.read_text(), DEFAULT_DATABASE_URL)
        metadata = DEFAULT_DATABASE_DIRECTORY.lstat()
        self.assertTrue(stat.S_ISDIR(metadata.st_mode))
        self.assertFalse(DEFAULT_DATABASE_DIRECTORY.is_symlink())
        self.assertEqual(metadata.st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o700)

    def test_production_without_an_explicit_database_fails_closed(self) -> None:
        result = self.hosted.run(environment_name="production")

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.hosted.alembic_capture.exists())
        self.assertFalse(self.hosted.uvicorn_capture.exists())
        self.assertIn("production", self.output(result).lower())
        self.assertIn("database", self.output(result).lower())

    def test_explicit_sqlite_and_postgresql_urls_reach_every_boundary_unchanged(
        self,
    ) -> None:
        database_urls = (
            "sqlite+pysqlite:////var/tmp/explicit-commerce-ops.sqlite3",
            "postgresql+psycopg://example.invalid:5432/commerce",
        )

        for database_url in database_urls:
            with self.subTest(database_url=database_url):
                self.hosted.alembic_capture.unlink(missing_ok=True)
                self.hosted.uvicorn_capture.unlink(missing_ok=True)

                result = self.hosted.run(
                    environment_name="production",
                    database_url=database_url,
                )

                self.assertEqual(result.returncode, 0, self.output(result))
                self.assertEqual(self.hosted.alembic_capture.read_text(), database_url)
                self.assertEqual(self.hosted.uvicorn_capture.read_text(), database_url)

    def test_playwright_prefers_linux_shared_memory_with_an_explicit_override(
        self,
    ) -> None:
        config = PLAYWRIGHT_CONFIG.read_text(encoding="utf-8")
        lifecycle = PLAYWRIGHT_DATABASE_LIFECYCLE.read_text(encoding="utf-8")

        self.assertRegex(config, r"from 'node:os'")
        self.assertRegex(config, r"\btmpdir\(\)")
        self.assertIn("COMMERCE_OPS_E2E_DATABASE_DIR", config)
        self.assertIn("'/dev/shm'", config)
        self.assertRegex(config, r"process\.platform\s*===\s*'linux'")
        self.assertRegex(config, r"\b(?:accessSync|statSync)\(")
        self.assertRegex(
            config,
            r"(?s)prepareE2EDatabase\(\{\s*directory: e2eDatabaseDirectory\(\)",
        )
        self.assertNotRegex(
            config,
            r"(?s)const databasePath = path\.join\(\s*productDirectory,\s*'data'",
        )
        self.assertIn("mkdtempSync", lifecycle)
        self.assertIn("realpathSync", lifecycle)
        self.assertIn("lstatSync", lifecycle)
        self.assertIn("COMMERCE_OPS_E2E_DATABASE_DIRECTORY_DEVICE", lifecycle)
        self.assertIn("COMMERCE_OPS_E2E_DATABASE_DIRECTORY_INODE", lifecycle)
        self.assertIn("COMMERCE_OPS_E2E_DATABASE_DIRECTORY_OWNER_UID", lifecycle)
        self.assertIn("0o700", lifecycle)
        self.assertIn("0o600", lifecycle)


if __name__ == "__main__":
    unittest.main()
