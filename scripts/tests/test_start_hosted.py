from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = PRODUCT_ROOT / "scripts" / "start-hosted.sh"


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
        with tempfile.TemporaryDirectory(prefix="commerce-ops-hosted-") as data_dir:
            environment = os.environ.copy()
            environment.update(
                {
                    "PORT": str(port),
                    "COMMERCE_OPS_ENVIRONMENT": "test",
                    "COMMERCE_OPS_DATABASE_URL": (
                        f"sqlite+pysqlite:///{Path(data_dir) / 'hosted.sqlite3'}"
                    ),
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

                self.assertEqual(health_status, 200)
                self.assertEqual(health_type, "application/json")
                self.assertEqual(json.loads(health_body)["status"], "ok")
                self.assertEqual(ready_status, 200)
                self.assertEqual(ready_type, "application/json")
                self.assertEqual(json.loads(ready_body)["status"], "ready")
                self.assertEqual(page_status, 200)
                self.assertEqual(page_type, "text/html")
                self.assertIn("CommerceOps Desk", page_body)

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
