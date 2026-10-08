from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = PRODUCT_ROOT / "scripts" / "start-hosted.sh"
EXPLICIT_SECRET = "explicit-session-secret-with-more-than-thirty-two-bytes"
CUSTOM_SECRET = "custom-file-session-secret-with-more-than-thirty-two-bytes"


class IsolatedHostedStart:
    """Run the real startup script with fake Alembic/Uvicorn boundaries."""

    def __init__(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="commerce-ops-secret-", dir="/var/tmp"
        )
        self.root = Path(self._temporary_directory.name)
        self.product_root = self.root / "product"
        self.script = self.product_root / "scripts" / "start-hosted.sh"
        self.venv_dir = self.root / "venv"
        self.default_secret_file = self.product_root / "data" / ".session-secret"

        self.script.parent.mkdir(parents=True)
        self.script.write_bytes(START_SCRIPT.read_bytes())
        self.script.chmod(0o700)
        (self.product_root / "frontend" / "dist").mkdir(parents=True)
        (self.product_root / "frontend" / "dist" / "index.html").write_text(
            "<!doctype html><title>test build</title>", encoding="utf-8"
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
  sleep "${{COMMERCE_OPS_TEST_ENTROPY_DELAY:-0}}"
  if [[ -n "${{COMMERCE_OPS_TEST_SWAP_SECRET_FILE:-}}" \
    && -n "${{COMMERCE_OPS_SESSION_SECRET_FILE:-}}" \
    && "${{3:-}}" == "$COMMERCE_OPS_SESSION_SECRET_FILE" ]]; then
    {real_python} "$@"
    mv -- "$COMMERCE_OPS_TEST_SWAP_SECRET_FILE" "$COMMERCE_OPS_SESSION_SECRET_FILE"
    exit 0
  fi
  exec {real_python} "$@"
fi

if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "alembic" ]]; then
  exit 0
fi

if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "uvicorn" ]]; then
  : "${{COMMERCE_OPS_TEST_CAPTURE_FILE:?capture file is required}}"
  printf '%s' "${{COMMERCE_OPS_SESSION_SECRET:-}}" \
    > "$COMMERCE_OPS_TEST_CAPTURE_FILE"
  if [[ -n "${{COMMERCE_OPS_TEST_UVICORN_ARGS_CAPTURE:-}}" ]]; then
    printf '%s\\0' "$@" > "$COMMERCE_OPS_TEST_UVICORN_ARGS_CAPTURE"
  fi
  exit 0
fi

echo "unexpected fake python invocation" >&2
exit 97
""",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)

    def environment(
        self,
        *,
        capture_name: str,
        overrides: dict[str, str] | None = None,
    ) -> tuple[dict[str, str], Path]:
        capture_file = self.root / capture_name
        environment = os.environ.copy()
        environment.pop("COMMERCE_OPS_SESSION_SECRET", None)
        environment.pop("COMMERCE_OPS_SESSION_SECRET_FILE", None)
        environment.update(
            {
                "PORT": "43210",
                "COMMERCE_OPS_VENV_DIR": str(self.venv_dir),
                "COMMERCE_OPS_ENVIRONMENT": "demo",
                "COMMERCE_OPS_TEST_CAPTURE_FILE": str(capture_file),
            }
        )
        if overrides:
            environment.update(overrides)
        return environment, capture_file

    def run(
        self,
        *,
        capture_name: str = "captured-secret",
        overrides: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Path]:
        environment, capture_file = self.environment(
            capture_name=capture_name,
            overrides=overrides,
        )
        result = subprocess.run(
            ["bash", str(self.script)],
            cwd=self.product_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result, capture_file


class HostedSessionSecretContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hosted = IsolatedHostedStart()

    def tearDown(self) -> None:
        self.hosted.cleanup()

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def assert_not_leaked(
        self,
        result: subprocess.CompletedProcess[str],
        *secret_values: str,
    ) -> None:
        output = self.output(result)
        for secret_value in secret_values:
            self.assertNotIn(secret_value, output)

    def test_hosted_uvicorn_explicitly_disables_access_logging(self) -> None:
        arguments_capture = self.hosted.root / "captured-uvicorn-arguments"

        result, _ = self.hosted.run(
            overrides={
                "COMMERCE_OPS_TEST_UVICORN_ARGS_CAPTURE": str(arguments_capture),
            }
        )

        self.assertEqual(result.returncode, 0, self.output(result))
        arguments = [
            value.decode("utf-8")
            for value in arguments_capture.read_bytes().split(b"\0")
            if value
        ]
        self.assertEqual(arguments[:3], ["-m", "uvicorn", "app.main:create_app"])
        self.assertIn("--no-access-log", arguments)

    def test_explicit_secret_takes_priority_without_reading_or_changing_a_file(
        self,
    ) -> None:
        ignored_file = self.hosted.root / "ignored-session-secret"
        ignored_value = "intentionally-invalid-file-value"
        ignored_file.write_text(ignored_value, encoding="utf-8")
        ignored_file.chmod(0o644)
        original_stat = ignored_file.stat()

        result, capture_file = self.hosted.run(
            overrides={
                "COMMERCE_OPS_SESSION_SECRET": EXPLICIT_SECRET,
                "COMMERCE_OPS_SESSION_SECRET_FILE": str(ignored_file),
            }
        )

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(capture_file.read_text(encoding="utf-8"), EXPLICIT_SECRET)
        self.assertEqual(ignored_file.read_text(encoding="utf-8"), ignored_value)
        self.assertEqual(stat.S_IMODE(ignored_file.stat().st_mode), 0o644)
        self.assertEqual(ignored_file.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertFalse(self.hosted.default_secret_file.exists())
        self.assert_not_leaked(result, EXPLICIT_SECRET, ignored_value)

    def test_data_directory_symlink_fails_without_changing_its_target_mode(
        self,
    ) -> None:
        external_directory = self.hosted.root / "external-data"
        external_directory.mkdir(mode=0o755)
        data_path = self.hosted.product_root / "data"
        data_path.symlink_to(external_directory, target_is_directory=True)

        result, capture_file = self.hosted.run(
            overrides={"COMMERCE_OPS_SESSION_SECRET": EXPLICIT_SECRET}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(capture_file.exists())
        self.assertTrue(data_path.is_symlink())
        self.assertEqual(stat.S_IMODE(external_directory.stat().st_mode), 0o755)
        self.assertIn("data directory", self.output(result).lower())
        self.assert_not_leaked(result, EXPLICIT_SECRET)

    def test_default_secret_is_generated_with_strong_urlsafe_bytes_and_private_modes(
        self,
    ) -> None:
        result, capture_file = self.hosted.run()

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertTrue(self.hosted.default_secret_file.is_file())
        secret = self.hosted.default_secret_file.read_text(encoding="ascii")
        self.assertGreaterEqual(len(secret), 64)
        self.assertRegex(secret, re.compile(r"^[A-Za-z0-9_-]+$"))
        self.assertNotIn("\n", secret)
        self.assertEqual(capture_file.read_text(encoding="ascii"), secret)
        self.assertEqual(
            stat.S_IMODE(self.hosted.default_secret_file.parent.stat().st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(self.hosted.default_secret_file.stat().st_mode),
            0o600,
        )
        self.assert_not_leaked(result, secret)

    def test_production_requires_an_explicit_secret_source(self) -> None:
        result, capture_file = self.hosted.run(
            overrides={
                "COMMERCE_OPS_ENVIRONMENT": "production",
                "COMMERCE_OPS_DATABASE_URL": (
                    f"sqlite+pysqlite:///{self.hosted.root / 'production.sqlite3'}"
                ),
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(capture_file.exists())
        self.assertFalse(self.hosted.default_secret_file.exists())
        self.assertIn("production", self.output(result).lower())
        self.assertIn("session secret", self.output(result).lower())

    def test_second_default_start_reuses_the_exact_existing_secret(self) -> None:
        first_result, first_capture = self.hosted.run(capture_name="first-capture")
        self.assertEqual(first_result.returncode, 0, self.output(first_result))
        original_bytes = self.hosted.default_secret_file.read_bytes()

        second_result, second_capture = self.hosted.run(capture_name="second-capture")

        self.assertEqual(second_result.returncode, 0, self.output(second_result))
        self.assertEqual(self.hosted.default_secret_file.read_bytes(), original_bytes)
        self.assertEqual(first_capture.read_bytes(), original_bytes)
        self.assertEqual(second_capture.read_bytes(), original_bytes)
        self.assertEqual(
            stat.S_IMODE(self.hosted.default_secret_file.stat().st_mode),
            0o600,
        )
        self.assert_not_leaked(second_result, original_bytes.decode("ascii"))

    def test_concurrent_first_starts_publish_one_complete_secret_atomically(
        self,
    ) -> None:
        start_barrier = threading.Barrier(2)

        def start(instance: str) -> tuple[subprocess.CompletedProcess[str], Path]:
            start_barrier.wait(timeout=5)
            return self.hosted.run(
                capture_name=f"capture-{instance}",
                overrides={"COMMERCE_OPS_TEST_ENTROPY_DELAY": "0.2"},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(start, instance) for instance in ("a", "b")]
            runs = [future.result(timeout=20) for future in futures]

        for result, _capture_file in runs:
            self.assertEqual(result.returncode, 0, self.output(result))
        published_secret = self.hosted.default_secret_file.read_bytes()
        self.assertGreaterEqual(len(published_secret), 64)
        self.assertEqual(
            [capture_file.read_bytes() for _result, capture_file in runs],
            [published_secret, published_secret],
        )
        self.assertEqual(
            stat.S_IMODE(self.hosted.default_secret_file.stat().st_mode),
            0o600,
        )
        for result, _capture_file in runs:
            self.assert_not_leaked(result, published_secret.decode("ascii"))

    def test_custom_secret_file_is_loaded_without_creating_the_default_file(
        self,
    ) -> None:
        custom_directory = self.hosted.root / "custom-secret-directory"
        custom_directory.mkdir(mode=0o700)
        custom_file = custom_directory / "session-secret"
        custom_file.write_text(CUSTOM_SECRET, encoding="ascii")
        custom_file.chmod(0o600)
        original_bytes = custom_file.read_bytes()

        result, capture_file = self.hosted.run(
            overrides={"COMMERCE_OPS_SESSION_SECRET_FILE": str(custom_file)}
        )

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(capture_file.read_bytes(), original_bytes)
        self.assertEqual(custom_file.read_bytes(), original_bytes)
        self.assertEqual(stat.S_IMODE(custom_file.stat().st_mode), 0o600)
        self.assertFalse(self.hosted.default_secret_file.exists())
        self.assert_not_leaked(result, CUSTOM_SECRET)

    def test_custom_secret_is_not_reopened_after_it_has_been_validated(self) -> None:
        custom_file = self.hosted.root / "session-secret"
        custom_file.write_text(CUSTOM_SECRET, encoding="ascii")
        custom_file.chmod(0o600)
        replacement_file = self.hosted.root / "replacement-secret"
        replacement_secret = (
            "replacement-session-secret-with-more-than-thirty-two-bytes"
        )
        replacement_file.write_text(replacement_secret, encoding="ascii")
        replacement_file.chmod(0o600)

        result, capture_file = self.hosted.run(
            overrides={
                "COMMERCE_OPS_SESSION_SECRET_FILE": str(custom_file),
                "COMMERCE_OPS_TEST_SWAP_SECRET_FILE": str(replacement_file),
            }
        )

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(capture_file.read_text(encoding="ascii"), CUSTOM_SECRET)
        self.assertEqual(custom_file.read_text(encoding="ascii"), replacement_secret)
        self.assertFalse(self.hosted.default_secret_file.exists())
        self.assert_not_leaked(result, CUSTOM_SECRET, replacement_secret)

    def test_missing_custom_secret_file_fails_closed_without_generating_one(
        self,
    ) -> None:
        missing_file = self.hosted.root / "missing" / "session-secret"

        result, capture_file = self.hosted.run(
            overrides={"COMMERCE_OPS_SESSION_SECRET_FILE": str(missing_file)}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(capture_file.exists())
        self.assertFalse(missing_file.exists())
        self.assertFalse(self.hosted.default_secret_file.exists())
        self.assertIn("session secret", self.output(result).lower())

    def test_short_custom_secret_file_fails_closed_without_leaking_it(self) -> None:
        custom_file = self.hosted.root / "short-session-secret"
        short_secret = "fewer-than-thirty-two-bytes"
        self.assertLess(len(short_secret.encode()), 32)
        custom_file.write_text(short_secret, encoding="ascii")
        custom_file.chmod(0o600)

        result, capture_file = self.hosted.run(
            overrides={"COMMERCE_OPS_SESSION_SECRET_FILE": str(custom_file)}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(capture_file.exists())
        self.assertEqual(custom_file.read_text(encoding="ascii"), short_secret)
        self.assertFalse(self.hosted.default_secret_file.exists())
        self.assert_not_leaked(result, short_secret)

    def test_overly_permissive_custom_secret_file_fails_closed_without_leaking_it(
        self,
    ) -> None:
        custom_file = self.hosted.root / "public-session-secret"
        custom_file.write_text(CUSTOM_SECRET, encoding="ascii")
        custom_file.chmod(0o640)

        result, capture_file = self.hosted.run(
            overrides={"COMMERCE_OPS_SESSION_SECRET_FILE": str(custom_file)}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(capture_file.exists())
        self.assertEqual(stat.S_IMODE(custom_file.stat().st_mode), 0o640)
        self.assertFalse(self.hosted.default_secret_file.exists())
        self.assert_not_leaked(result, CUSTOM_SECRET)


if __name__ == "__main__":
    unittest.main()
