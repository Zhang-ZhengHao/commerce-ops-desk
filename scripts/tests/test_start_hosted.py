from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = PRODUCT_ROOT / "scripts" / "start-hosted.sh"
TEST_SESSION_SECRET = "test-only-hosted-session-secret-at-least-32-bytes"


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def request(url: str) -> tuple[int, str, str]:
    with urllib.request.urlopen(url, timeout=1) as response:
        return (
            response.status,
            response.headers.get_content_type(),
            response.read().decode("utf-8"),
        )


def post_workspace(
    url: str,
    *,
    origin: str,
    forwarded_for: str,
    idempotency_key: str,
) -> int:
    payload = json.dumps({"initial_role": "manager"}).encode("utf-8")
    request_object = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "Origin": origin,
            "X-Forwarded-For": forwarded_for,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request_object, timeout=2) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code


def wait_for(url: str, process: subprocess.Popen[str]) -> tuple[int, str, str]:
    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"hosted process exited early ({process.returncode})\nstdout={stdout}\nstderr={stderr}"
            )
        try:
            return request(url)
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            time.sleep(0.1)
    raise AssertionError(f"hosted service did not become ready: {last_error}")


class HostedStartContractTest(unittest.TestCase):
    def test_missing_port_fails_with_an_actionable_message(self) -> None:
        environment = os.environ.copy()
        environment.pop("PORT", None)
        environment["COMMERCE_OPS_SESSION_SECRET"] = TEST_SESSION_SECRET

        result = subprocess.run(
            ["bash", str(START_SCRIPT)],
            cwd=PRODUCT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PORT", result.stderr)

    def test_random_port_serves_health_readiness_and_frontend(self) -> None:
        port = reserve_port()
        with tempfile.TemporaryDirectory(
            prefix="commerce-ops-hosted-", dir="/var/tmp"
        ) as data_dir:
            database_path = Path(data_dir) / "hosted.sqlite3"
            environment = os.environ.copy()
            environment.update(
                {
                    "PORT": str(port),
                    "COMMERCE_OPS_VENV_DIR": str(Path(sys.executable).parent.parent),
                    "COMMERCE_OPS_ENVIRONMENT": "demo",
                    "COMMERCE_OPS_DATABASE_URL": (
                        f"sqlite+pysqlite:///{database_path}"
                    ),
                    "COMMERCE_OPS_DEMO_SOURCE_HOURLY_LIMIT": "1",
                    "COMMERCE_OPS_SESSION_SECRET": TEST_SESSION_SECRET,
                    "COMMERCE_OPS_WEBHOOK_ENABLED": "false",
                }
            )
            process = subprocess.Popen(
                ["bash", str(START_SCRIPT)],
                cwd=PRODUCT_ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                health_status, health_type, health_body = wait_for(
                    f"http://127.0.0.1:{port}/health", process
                )
                ready_status, ready_type, ready_body = request(
                    f"http://127.0.0.1:{port}/ready"
                )
                page_status, page_type, page_body = request(f"http://127.0.0.1:{port}/")
                origin = f"http://127.0.0.1:{port}"
                first_workspace_status = post_workspace(
                    f"{origin}/api/demo/workspaces",
                    origin=origin,
                    forwarded_for="198.51.100.71",
                    idempotency_key="hosted-proxy-boundary-one",
                )
                second_workspace_status = post_workspace(
                    f"{origin}/api/demo/workspaces",
                    origin=origin,
                    forwarded_for="198.51.100.72",
                    idempotency_key="hosted-proxy-boundary-two",
                )

                self.assertEqual(health_status, 200)
                self.assertEqual(health_type, "application/json")
                self.assertEqual(json.loads(health_body)["status"], "ok")
                self.assertEqual(ready_status, 200)
                self.assertEqual(ready_type, "application/json")
                self.assertEqual(json.loads(ready_body)["status"], "ready")
                self.assertEqual(page_status, 200)
                self.assertEqual(page_type, "text/html")
                self.assertIn("CommerceOps Desk", page_body)
                self.assertEqual(first_workspace_status, 201)
                self.assertEqual(second_workspace_status, 429)

                with sqlite3.connect(database_path) as connection:
                    revision = connection.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchone()
                self.assertEqual(revision, ("0006_maintenance_indexes",))

                container_address = socket.gethostbyname(socket.gethostname())
                if not container_address.startswith("127."):
                    external_status, _, _ = request(
                        f"http://{container_address}:{port}/health"
                    )
                    self.assertEqual(external_status, 200)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
