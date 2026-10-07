from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = PRODUCT_ROOT / "scripts" / "setup-runtime.sh"


class RuntimeSetupContractTest(unittest.TestCase):
    def test_python_runtime_rebuilds_offline_and_then_skips_when_current(self) -> None:
        self.assertTrue(SETUP_SCRIPT.is_file(), "setup-runtime.sh must exist")
        wheelhouse_dir = PRODUCT_ROOT / "backend" / "wheelhouse"
        self.assertTrue(
            list(wheelhouse_dir.glob("alembic-*.whl")),
            "make setup must prepare the ignored recovery wheelhouse",
        )

        with tempfile.TemporaryDirectory(
            prefix="commerce-ops-setup-", dir="/var/tmp"
        ) as temporary_root:
            temporary_path = Path(temporary_root)
            venv_dir = temporary_path / "venv"
            environment = os.environ.copy()
            environment.update(
                {
                    "COMMERCE_OPS_VENV_DIR": str(venv_dir),
                    "COMMERCE_OPS_SETUP_LOCK": str(temporary_path / "setup.lock"),
                    "COMMERCE_OPS_SETUP_FRONTEND": "0",
                    "COMMERCE_OPS_OFFLINE_ONLY": "1",
                }
            )

            first_run = subprocess.run(
                ["bash", str(SETUP_SCRIPT)],
                cwd=PRODUCT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

            self.assertEqual(first_run.returncode, 0, first_run.stderr)
            self.assertTrue((venv_dir / ".requirements.sha256").is_file())
            import_check = subprocess.run(
                [
                    str(venv_dir / "bin" / "python"),
                    "-c",
                    "import alembic, fastapi, psycopg, pydantic_settings, sqlalchemy, uvicorn",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(import_check.returncode, 0, import_check.stderr)

            second_run = subprocess.run(
                ["bash", str(SETUP_SCRIPT)],
                cwd=PRODUCT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

            self.assertEqual(second_run.returncode, 0, second_run.stderr)
            self.assertIn("Python runtime already current", second_run.stdout)

            alembic_packages = list(venv_dir.glob("lib/python*/site-packages/alembic"))
            self.assertEqual(len(alembic_packages), 1)
            shutil.rmtree(alembic_packages[0])

            repair_run = subprocess.run(
                ["bash", str(SETUP_SCRIPT)],
                cwd=PRODUCT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

            self.assertEqual(repair_run.returncode, 0, repair_run.stderr)
            self.assertIn("Python runtime rebuilt from wheelhouse", repair_run.stdout)
            repaired_import = subprocess.run(
                [str(venv_dir / "bin" / "python"), "-c", "import alembic"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(repaired_import.returncode, 0, repaired_import.stderr)


if __name__ == "__main__":
    unittest.main()
