from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_start_hosted_session_secret import EXPLICIT_SECRET, IsolatedHostedStart

EXPLICIT_WEBHOOK_SECRET = (
    "explicit-webhook-master-secret-with-more-than-thirty-two-bytes"
)
FILE_WEBHOOK_SECRET = "file-webhook-master-secret-with-more-than-thirty-two-bytes"
UNSET = "<unset>"


class WebhookHostedStart(IsolatedHostedStart):
    @property
    def default_webhook_secret_file(self) -> Path:
        return self.product_root / "data" / ".webhook-secret"

    def _write_fake_python(self) -> None:
        fake_python = self.venv_dir / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        real_python = shlex.quote(sys.executable)
        fake_python.write_text(
            rf"""#!/usr/bin/env bash
set -euo pipefail

if [[ "${{1:-}}" == "-c" ]]; then
  sleep "${{COMMERCE_OPS_TEST_ENTROPY_DELAY:-0}}"
  exec {real_python} "$@"
fi

if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "alembic" ]]; then
  exit 0
fi

if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "uvicorn" ]]; then
  : "${{COMMERCE_OPS_TEST_CAPTURE_FILE:?capture file is required}}"
  if [[ -v COMMERCE_OPS_WEBHOOK_ENABLED ]]; then
    webhook_enabled="$COMMERCE_OPS_WEBHOOK_ENABLED"
  else
    webhook_enabled={shlex.quote(UNSET)}
  fi
  if [[ -v COMMERCE_OPS_WEBHOOK_MASTER_SECRET ]]; then
    webhook_secret="$COMMERCE_OPS_WEBHOOK_MASTER_SECRET"
  else
    webhook_secret={shlex.quote(UNSET)}
  fi
  printf '%s\0%s' "$webhook_enabled" "$webhook_secret" \
    > "$COMMERCE_OPS_TEST_CAPTURE_FILE"
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
        environment, capture_file = super().environment(
            capture_name=capture_name,
            overrides=None,
        )
        for name in (
            "COMMERCE_OPS_WEBHOOK_ENABLED",
            "COMMERCE_OPS_WEBHOOK_MASTER_SECRET",
            "COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE",
        ):
            environment.pop(name, None)
        environment["COMMERCE_OPS_SESSION_SECRET"] = EXPLICIT_SECRET
        if overrides:
            environment.update(overrides)
        return environment, capture_file


class HostedWebhookSecretContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hosted = WebhookHostedStart()

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

    @staticmethod
    def captured_values(capture_file: Path) -> tuple[str, str]:
        enabled, secret = capture_file.read_bytes().split(b"\0", maxsplit=1)
        return enabled.decode("utf-8"), secret.decode("utf-8")

    def production_environment(self) -> dict[str, str]:
        return {
            "COMMERCE_OPS_ENVIRONMENT": "production",
            "COMMERCE_OPS_DATABASE_URL": (
                f"sqlite+pysqlite:///{self.hosted.root / 'production.sqlite3'}"
            ),
        }

    def test_demo_defaults_webhook_on_and_generates_one_private_secret(self) -> None:
        result, capture_file = self.hosted.run()

        self.assertEqual(result.returncode, 0, self.output(result))
        enabled, captured_secret = self.captured_values(capture_file)
        published_secret = self.hosted.default_webhook_secret_file.read_text(
            encoding="ascii"
        )
        self.assertEqual(enabled, "true")
        self.assertEqual(captured_secret, published_secret)
        self.assertGreaterEqual(len(published_secret.encode("ascii")), 32)
        self.assertEqual(
            stat.S_IMODE(self.hosted.default_webhook_secret_file.stat().st_mode),
            0o600,
        )
        self.assertEqual(
            self.hosted.default_webhook_secret_file.stat().st_uid,
            os.geteuid(),
        )
        self.assert_not_leaked(result, published_secret)

    def test_explicit_demo_disable_does_not_read_or_create_a_secret(self) -> None:
        missing_file = self.hosted.root / "must-not-be-read"

        result, capture_file = self.hosted.run(
            overrides={
                "COMMERCE_OPS_WEBHOOK_ENABLED": "false",
                "COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE": str(missing_file),
            }
        )

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(self.captured_values(capture_file), ("false", UNSET))
        self.assertFalse(missing_file.exists())
        self.assertFalse(self.hosted.default_webhook_secret_file.exists())

    def test_explicit_webhook_switch_accepts_only_true_or_false(self) -> None:
        result, capture_file = self.hosted.run(
            overrides={"COMMERCE_OPS_WEBHOOK_ENABLED": "yes"}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(capture_file.exists())
        self.assertFalse(self.hosted.default_webhook_secret_file.exists())
        self.assertIn("true or false", self.output(result).lower())

    def test_production_defaults_webhook_off_without_a_secret(self) -> None:
        result, capture_file = self.hosted.run(overrides=self.production_environment())

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(self.captured_values(capture_file), ("false", UNSET))
        self.assertFalse(self.hosted.default_webhook_secret_file.exists())

    def test_enabled_production_requires_an_explicit_webhook_secret_source(
        self,
    ) -> None:
        result, capture_file = self.hosted.run(
            overrides={
                **self.production_environment(),
                "COMMERCE_OPS_WEBHOOK_ENABLED": "true",
            }
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(capture_file.exists())
        self.assertFalse(self.hosted.default_webhook_secret_file.exists())
        self.assertIn("webhook", self.output(result).lower())
        self.assertIn("secret source", self.output(result).lower())

    def test_direct_secret_takes_priority_without_reading_the_file(self) -> None:
        ignored_file = self.hosted.root / "ignored-webhook-secret"
        ignored_value = "intentionally-invalid-file-value"
        ignored_file.write_text(ignored_value, encoding="utf-8")
        ignored_file.chmod(0o644)
        original_stat = ignored_file.stat()

        result, capture_file = self.hosted.run(
            overrides={
                "COMMERCE_OPS_WEBHOOK_MASTER_SECRET": EXPLICIT_WEBHOOK_SECRET,
                "COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE": str(ignored_file),
            }
        )

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(
            self.captured_values(capture_file),
            ("true", EXPLICIT_WEBHOOK_SECRET),
        )
        self.assertEqual(ignored_file.read_text(encoding="utf-8"), ignored_value)
        self.assertEqual(stat.S_IMODE(ignored_file.stat().st_mode), 0o644)
        self.assertEqual(ignored_file.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertFalse(self.hosted.default_webhook_secret_file.exists())
        self.assert_not_leaked(result, EXPLICIT_WEBHOOK_SECRET, ignored_value)

    def test_present_but_empty_or_short_direct_secret_never_falls_back(self) -> None:
        fallback_file = self.hosted.root / "valid-but-ignored-webhook-secret"
        fallback_file.write_text(FILE_WEBHOOK_SECRET, encoding="utf-8")
        fallback_file.chmod(0o600)

        for case_name, invalid_secret in (("empty", ""), ("short", "too-short")):
            with self.subTest(case_name=case_name):
                result, capture_file = self.hosted.run(
                    capture_name=f"capture-{case_name}",
                    overrides={
                        "COMMERCE_OPS_WEBHOOK_MASTER_SECRET": invalid_secret,
                        "COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE": str(fallback_file),
                    },
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(capture_file.exists())
                self.assertFalse(self.hosted.default_webhook_secret_file.exists())
                self.assert_not_leaked(result, FILE_WEBHOOK_SECRET)
                if invalid_secret:
                    self.assert_not_leaked(result, invalid_secret)

    def test_direct_secret_rejects_invalid_utf8_without_leaking_printable_bytes(
        self,
    ) -> None:
        printable_marker = "direct-invalid-utf8-webhook-secret-marker"
        invalid_secret = os.fsdecode(printable_marker.encode("ascii") + b"\xff")

        result, capture_file = self.hosted.run(
            overrides={"COMMERCE_OPS_WEBHOOK_MASTER_SECRET": invalid_secret}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(capture_file.exists())
        self.assertFalse(self.hosted.default_webhook_secret_file.exists())
        self.assert_not_leaked(result, printable_marker)

    def test_custom_file_accepts_no_ending_one_lf_or_one_crlf(self) -> None:
        for case_name, ending in (("none", b""), ("lf", b"\n"), ("crlf", b"\r\n")):
            with self.subTest(case_name=case_name):
                custom_file = self.hosted.root / f"webhook-secret-{case_name}"
                original_bytes = FILE_WEBHOOK_SECRET.encode("utf-8") + ending
                custom_file.write_bytes(original_bytes)
                custom_file.chmod(0o600)

                result, capture_file = self.hosted.run(
                    capture_name=f"capture-{case_name}",
                    overrides={
                        "COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE": str(custom_file)
                    },
                )

                self.assertEqual(result.returncode, 0, self.output(result))
                self.assertEqual(
                    self.captured_values(capture_file),
                    ("true", FILE_WEBHOOK_SECRET),
                )
                self.assertEqual(custom_file.read_bytes(), original_bytes)
                self.assertFalse(self.hosted.default_webhook_secret_file.exists())
                self.assert_not_leaked(result, FILE_WEBHOOK_SECRET)

    def test_custom_file_rejects_ambiguous_or_unsafe_bytes_without_leaking(
        self,
    ) -> None:
        invalid_values = {
            "double-lf": FILE_WEBHOOK_SECRET.encode() + b"\n\n",
            "lone-cr": FILE_WEBHOOK_SECRET.encode() + b"\r",
            "leading-space": b" " + FILE_WEBHOOK_SECRET.encode(),
            "trailing-space": FILE_WEBHOOK_SECRET.encode() + b" \n",
            "nul": FILE_WEBHOOK_SECRET.encode() + b"\x00",
            "invalid-utf8": FILE_WEBHOOK_SECRET.encode() + b"\xff",
            "short": b"x" * 31,
        }

        for case_name, invalid_bytes in invalid_values.items():
            with self.subTest(case_name=case_name):
                custom_file = self.hosted.root / f"invalid-{case_name}"
                custom_file.write_bytes(invalid_bytes)
                custom_file.chmod(0o600)

                result, capture_file = self.hosted.run(
                    capture_name=f"capture-invalid-{case_name}",
                    overrides={
                        "COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE": str(custom_file)
                    },
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(capture_file.exists())
                self.assertEqual(custom_file.read_bytes(), invalid_bytes)
                self.assertFalse(self.hosted.default_webhook_secret_file.exists())
                self.assert_not_leaked(result, FILE_WEBHOOK_SECRET)
                if case_name not in {"invalid-utf8", "nul"}:
                    self.assert_not_leaked(
                        result,
                        invalid_bytes.decode("utf-8", errors="ignore"),
                    )

    def test_custom_file_rejects_symlinks_and_public_permissions(self) -> None:
        target = self.hosted.root / "webhook-secret-target"
        target.write_text(FILE_WEBHOOK_SECRET, encoding="utf-8")
        target.chmod(0o600)
        symlink = self.hosted.root / "webhook-secret-symlink"
        symlink.symlink_to(target)

        public_file = self.hosted.root / "public-webhook-secret"
        public_file.write_text(FILE_WEBHOOK_SECRET, encoding="utf-8")
        public_file.chmod(0o640)

        for case_name, secret_file in (("symlink", symlink), ("mode", public_file)):
            with self.subTest(case_name=case_name):
                result, capture_file = self.hosted.run(
                    capture_name=f"capture-{case_name}",
                    overrides={
                        "COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE": str(secret_file)
                    },
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(capture_file.exists())
                self.assertFalse(self.hosted.default_webhook_secret_file.exists())
                self.assert_not_leaked(result, FILE_WEBHOOK_SECRET)

        self.assertTrue(symlink.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), FILE_WEBHOOK_SECRET)
        self.assertEqual(stat.S_IMODE(public_file.stat().st_mode), 0o640)

    def test_custom_file_rejects_a_secret_not_owned_by_the_service_user(self) -> None:
        candidates = (Path("/etc/hostname"), Path("/etc/passwd"))
        wrong_owner_file = next(
            (
                candidate
                for candidate in candidates
                if candidate.is_file() and candidate.stat().st_uid != os.geteuid()
            ),
            None,
        )
        self.assertIsNotNone(wrong_owner_file)
        assert wrong_owner_file is not None

        result, capture_file = self.hosted.run(
            overrides={"COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE": str(wrong_owner_file)}
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(capture_file.exists())
        self.assertFalse(self.hosted.default_webhook_secret_file.exists())
        self.assertIn("owned by the service user", self.output(result).lower())

    def test_second_demo_start_reuses_the_exact_generated_secret(self) -> None:
        first_result, first_capture = self.hosted.run(capture_name="first-capture")
        self.assertEqual(first_result.returncode, 0, self.output(first_result))
        original_bytes = self.hosted.default_webhook_secret_file.read_bytes()

        second_result, second_capture = self.hosted.run(capture_name="second-capture")

        self.assertEqual(second_result.returncode, 0, self.output(second_result))
        self.assertEqual(
            self.hosted.default_webhook_secret_file.read_bytes(), original_bytes
        )
        self.assertEqual(
            self.captured_values(first_capture)[1].encode(), original_bytes
        )
        self.assertEqual(
            self.captured_values(second_capture)[1].encode(), original_bytes
        )
        self.assert_not_leaked(second_result, original_bytes.decode("ascii"))

    def test_concurrent_demo_first_starts_publish_one_complete_secret(self) -> None:
        start_barrier = threading.Barrier(2)

        def start(instance: str) -> tuple[subprocess.CompletedProcess[str], Path]:
            start_barrier.wait(timeout=5)
            return self.hosted.run(
                capture_name=f"capture-{instance}",
                overrides={"COMMERCE_OPS_TEST_ENTROPY_DELAY": "0.1"},
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(start, instance) for instance in ("a", "b")]
            runs = [future.result(timeout=20) for future in futures]

        for result, _capture_file in runs:
            self.assertEqual(result.returncode, 0, self.output(result))
        published_secret = self.hosted.default_webhook_secret_file.read_bytes()
        self.assertGreaterEqual(len(published_secret), 32)
        self.assertEqual(
            [
                self.captured_values(capture_file)[1].encode()
                for _result, capture_file in runs
            ],
            [published_secret, published_secret],
        )
        self.assertEqual(
            stat.S_IMODE(self.hosted.default_webhook_secret_file.stat().st_mode),
            0o600,
        )
        for result, _capture_file in runs:
            self.assert_not_leaked(result, published_secret.decode("ascii"))


if __name__ == "__main__":
    unittest.main()
