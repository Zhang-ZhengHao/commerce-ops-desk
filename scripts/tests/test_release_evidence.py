from __future__ import annotations

import json
import subprocess
import sys
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PRODUCT_ROOT = Path(__file__).resolve().parents[2]
SELECTOR = PRODUCT_ROOT / "scripts" / "select_release_evidence.py"
REPOSITORY = "example/commerce-ops-desk"
DEPLOY_SHA = "0123456789abcdef0123456789abcdef01234567"
VERIFY_WORKFLOW_ID = 377_910_314
CODEQL_WORKFLOW_ID = 378_862_582


def workflow_run(
    *,
    workflow: str = "Verify",
    workflow_database_id: int = VERIFY_WORKFLOW_ID,
    run_id: int = 10_001,
    **overrides: Any,
) -> dict[str, Any]:
    run: dict[str, Any] = {
        "attempt": 1,
        "conclusion": "success",
        "databaseId": run_id,
        "event": "push",
        "headBranch": "main",
        "headSha": DEPLOY_SHA,
        "status": "completed",
        "url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        "workflowName": workflow,
        "workflowDatabaseId": workflow_database_id,
    }
    run.update(overrides)
    return run


def codeql_run(*, run_id: int = 20_002, **overrides: Any) -> dict[str, Any]:
    return workflow_run(
        workflow="CodeQL",
        workflow_database_id=CODEQL_WORKFLOW_ID,
        run_id=run_id,
        event="dynamic",
        **overrides,
    )


class ReleaseEvidenceSelectorTest(unittest.TestCase):
    def run_selector(
        self,
        payload: object,
        *,
        repository: str = REPOSITORY,
        deploy_sha: str = DEPLOY_SHA,
        required_workflows: Sequence[str] = (
            f"Verify={VERIFY_WORKFLOW_ID}@push",
            f"CodeQL={CODEQL_WORKFLOW_ID}@dynamic",
        ),
    ) -> subprocess.CompletedProcess[str]:
        arguments = [
            sys.executable,
            str(SELECTOR),
            "--repository",
            repository,
            "--deploy-sha",
            deploy_sha,
            "--input",
            "-",
        ]
        for required_workflow in required_workflows:
            arguments.extend(("--require-workflow", required_workflow))
        return subprocess.run(
            arguments,
            cwd=PRODUCT_ROOT,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    @staticmethod
    def output(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout + result.stderr

    def assert_rejected(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertNotEqual(result.returncode, 0, self.output(result))
        self.assertEqual(result.stdout, "")

    def test_selects_one_exact_run_per_required_workflow_in_argument_order(
        self,
    ) -> None:
        codeql = workflow_run(
            workflow="CodeQL",
            workflow_database_id=CODEQL_WORKFLOW_ID,
            run_id=20_002,
            attempt=2,
            event="dynamic",
            displayTitle="attacker-controlled title",
        )
        verify = workflow_run(
            run_id=20_001,
            attempt=3,
            displayTitle="untrusted verify title",
        )
        unrelated = workflow_run(
            workflow="Other workflow",
            workflow_database_id=999_999,
            run_id=20_003,
        )

        result = self.run_selector([codeql, unrelated, verify])

        self.assertEqual(result.returncode, 0, self.output(result))
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            json.loads(result.stdout),
            {
                "deploy_sha": DEPLOY_SHA,
                "evidence": [
                    {
                        "workflow": "Verify",
                        "workflow_database_id": VERIFY_WORKFLOW_ID,
                        "run_id": 20_001,
                        "attempt": 3,
                        "url": (
                            "https://github.com/example/commerce-ops-desk/"
                            "actions/runs/20001/attempts/3"
                        ),
                    },
                    {
                        "workflow": "CodeQL",
                        "workflow_database_id": CODEQL_WORKFLOW_ID,
                        "run_id": 20_002,
                        "attempt": 2,
                        "url": (
                            "https://github.com/example/commerce-ops-desk/"
                            "actions/runs/20002/attempts/2"
                        ),
                    },
                ],
            },
        )
        self.assertNotIn("displayTitle", result.stdout)
        self.assertNotIn("attacker-controlled", result.stdout)
        self.assertNotIn("untrusted verify title", result.stdout)

    def test_rejects_the_github_api_result_cap_as_a_truncation_sentinel(
        self,
    ) -> None:
        runs = [
            workflow_run(),
            codeql_run(),
            *[
                workflow_run(
                    workflow="Other workflow",
                    workflow_database_id=999_999,
                    run_id=30_000 + index,
                )
                for index in range(997)
            ],
        ]

        accepted = self.run_selector(runs)

        self.assertEqual(len(runs), 999)
        self.assertEqual(accepted.returncode, 0, self.output(accepted))

        for total in (1_000, 1_001):
            with self.subTest(total=total):
                capped_runs = [
                    *runs,
                    *[
                        workflow_run(
                            workflow="Other workflow",
                            workflow_database_id=999_999,
                            run_id=31_000 + index,
                        )
                        for index in range(total - len(runs))
                    ],
                ]

                rejected = self.run_selector(capped_runs)

                self.assertEqual(len(capped_runs), total)
                self.assertEqual(rejected.returncode, 2, self.output(rejected))
                self.assertEqual(rejected.stdout, "")
                self.assertEqual(
                    rejected.stderr,
                    "release-evidence: ERROR (invalid selector input)\n",
                )

    def test_requires_a_full_lowercase_deploy_sha(self) -> None:
        for invalid_sha in (DEPLOY_SHA[:-1], DEPLOY_SHA.upper(), "g" * 40):
            with self.subTest(deploy_sha=invalid_sha):
                result = self.run_selector([], deploy_sha=invalid_sha)

                self.assert_rejected(result)

    def test_requires_a_canonical_owner_and_repository_name(self) -> None:
        for invalid_repository in (
            "example",
            "https://github.com/example/commerce-ops-desk",
            "example/commerce-ops-desk.git",
            "example/../commerce-ops-desk",
        ):
            with self.subTest(repository=invalid_repository):
                result = self.run_selector([], repository=invalid_repository)

                self.assert_rejected(result)

    def test_requires_unique_exact_workflow_name_and_database_id_pairs(self) -> None:
        invalid_requirements = (
            ("Verify",),
            (f"Verify={VERIFY_WORKFLOW_ID}",),
            ("Verify=0@push",),
            (f"Verify={VERIFY_WORKFLOW_ID}@",),
            (f"Verify={VERIFY_WORKFLOW_ID}@push@dynamic",),
            (f" Verify={VERIFY_WORKFLOW_ID}@push",),
            (f"Verify={VERIFY_WORKFLOW_ID}@ push",),
            (
                f"Verify={VERIFY_WORKFLOW_ID}@push",
                f"Verify={CODEQL_WORKFLOW_ID}@dynamic",
            ),
            (
                f"Verify={VERIFY_WORKFLOW_ID}@push",
                f"CodeQL={VERIFY_WORKFLOW_ID}@dynamic",
            ),
        )
        for requirements in invalid_requirements:
            with self.subTest(requirements=requirements):
                result = self.run_selector([], required_workflows=requirements)

                self.assert_rejected(result)

    def test_requires_exactly_the_release_workflow_policy(self) -> None:
        invalid_requirements = (
            (f"Verify={VERIFY_WORKFLOW_ID}@push",),
            (f"CodeQL={CODEQL_WORKFLOW_ID}@dynamic",),
            (
                f"Verify={VERIFY_WORKFLOW_ID}@push",
                f"CodeQL={CODEQL_WORKFLOW_ID}@dynamic",
                "Other=999999@push",
            ),
            (
                f"Verify={VERIFY_WORKFLOW_ID}@push",
                f"Other={CODEQL_WORKFLOW_ID}@dynamic",
            ),
            (
                f"Verify={VERIFY_WORKFLOW_ID}@workflow_dispatch",
                f"CodeQL={CODEQL_WORKFLOW_ID}@dynamic",
            ),
            (
                f"Verify={VERIFY_WORKFLOW_ID}@push",
                f"CodeQL={CODEQL_WORKFLOW_ID}@push",
            ),
        )
        for requirements in invalid_requirements:
            with self.subTest(requirements=requirements):
                result = self.run_selector(
                    [workflow_run(), codeql_run()],
                    required_workflows=requirements,
                )

                self.assert_rejected(result)

    def test_rejects_missing_required_workflow_evidence(self) -> None:
        result = self.run_selector(
            [workflow_run()],
            required_workflows=(
                f"Verify={VERIFY_WORKFLOW_ID}@push",
                f"CodeQL={CODEQL_WORKFLOW_ID}@dynamic",
            ),
        )

        self.assert_rejected(result)

    def test_rejects_ambiguous_required_workflow_evidence(self) -> None:
        result = self.run_selector(
            [
                workflow_run(run_id=30_001),
                workflow_run(run_id=30_002),
                codeql_run(),
            ],
        )

        self.assert_rejected(result)

    def test_accepts_only_main_push_runs_for_the_exact_sha(self) -> None:
        ineligible_overrides: Sequence[Mapping[str, object]] = (
            {"event": "pull_request"},
            {"headBranch": "release"},
            {"headSha": "1123456789abcdef0123456789abcdef01234567"},
            {"headSha": DEPLOY_SHA.upper()},
        )
        for overrides in ineligible_overrides:
            with self.subTest(overrides=overrides):
                run = workflow_run()
                run.update(overrides)
                result = self.run_selector(
                    [run, codeql_run()],
                )

                self.assert_rejected(result)

    def test_accepts_only_completed_successful_runs(self) -> None:
        for overrides in (
            {"status": "in_progress", "conclusion": ""},
            {"status": "completed", "conclusion": "failure"},
            {"status": "completed", "conclusion": "cancelled"},
        ):
            with self.subTest(overrides=overrides):
                run = workflow_run()
                run.update(overrides)
                result = self.run_selector(
                    [run, codeql_run()],
                )

                self.assert_rejected(result)

    def test_matches_both_workflow_name_and_database_id_exactly(self) -> None:
        for overrides in (
            {"workflowName": "verify"},
            {"workflowName": "Verify "},
            {"workflowDatabaseId": CODEQL_WORKFLOW_ID},
            {"workflowDatabaseId": str(VERIFY_WORKFLOW_ID)},
        ):
            with self.subTest(overrides=overrides):
                result = self.run_selector(
                    [workflow_run(**overrides), codeql_run()],
                )

                self.assert_rejected(result)

    def test_requires_the_canonical_base_url_for_the_same_numeric_run_id(
        self,
    ) -> None:
        invalid_urls = (
            "http://github.com/example/commerce-ops-desk/actions/runs/10001",
            "https://github.example/example/commerce-ops-desk/actions/runs/10001",
            "https://github.com/other/commerce-ops-desk/actions/runs/10001",
            "https://github.com/example/commerce-ops-desk/actions/runs/10002",
            "https://github.com/example/commerce-ops-desk/actions/runs/10001/",
            "https://github.com/example/commerce-ops-desk/actions/runs/10001?check=1",
            "https://github.com/example/commerce-ops-desk/actions/runs/10001/attempts/1",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                result = self.run_selector(
                    [workflow_run(url=url), codeql_run()],
                )

                self.assert_rejected(result)

    def test_rejects_non_numeric_run_ids_and_malformed_json_shapes(self) -> None:
        missing_attempt = workflow_run()
        del missing_attempt["attempt"]
        for payload in (
            {"workflow_runs": [workflow_run()]},
            [missing_attempt],
            [workflow_run(attempt=0)],
            [workflow_run(attempt=True)],
            [workflow_run(attempt="1")],
            [workflow_run(databaseId="10001")],
            [workflow_run(databaseId=True)],
            [workflow_run(url=None)],
            ["not a run"],
        ):
            with self.subTest(payload=payload):
                result = self.run_selector(
                    (
                        [*payload, codeql_run()]
                        if isinstance(payload, list)
                        else payload
                    ),
                )

                self.assert_rejected(result)

    def test_rejects_events_assigned_to_the_wrong_required_workflow(self) -> None:
        verify = workflow_run(event="dynamic")
        codeql = workflow_run(
            workflow="CodeQL",
            workflow_database_id=CODEQL_WORKFLOW_ID,
            run_id=20_002,
            event="push",
        )

        result = self.run_selector([verify, codeql])

        self.assert_rejected(result)

    def test_rejects_an_event_other_than_the_one_required_for_the_workflow(
        self,
    ) -> None:
        result = self.run_selector(
            [workflow_run(event="workflow_dispatch"), codeql_run()],
        )

        self.assert_rejected(result)


if __name__ == "__main__":
    unittest.main()
