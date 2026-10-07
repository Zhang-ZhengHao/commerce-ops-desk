from __future__ import annotations

import unittest
from pathlib import Path

import tomllib

PRODUCT_ROOT = Path(__file__).resolve().parents[2]


class AppManifestContractTest(unittest.TestCase):
    def test_manifest_declares_the_verified_hosted_entrypoint(self) -> None:
        manifest_path = PRODUCT_ROOT / "app.toml"
        self.assertTrue(manifest_path.is_file(), "app.toml must exist")
        with manifest_path.open("rb") as manifest_file:
            manifest = tomllib.load(manifest_file)

        self.assertEqual(manifest["name"], "CommerceOps Desk")
        self.assertEqual(manifest["start"], "bash scripts/start-hosted.sh")
        self.assertEqual(manifest["health"], "/health")
        self.assertNotIn("pip install", manifest["start"])
        self.assertNotIn("npm install", manifest["start"])


if __name__ == "__main__":
    unittest.main()
