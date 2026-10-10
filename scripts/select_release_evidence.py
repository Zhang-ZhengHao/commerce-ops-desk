#!/usr/bin/env python3
"""Select immutable GitHub Actions evidence for one exact release commit."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
POSITIVE_INTEGER_PATTERN = re.compile(r"[1-9][0-9]*\Z")
EVENT_PATTERN = re.compile(r"[A-Za-z0-9_]+\Z")
REQUIRED_WORKFLOW_EVENTS = {"Verify": "push", "CodeQL": "dynamic"}
GITHUB_FILTERED_RESULT_CAP = 1_000


class EvidenceInputError(RuntimeError):
    """Raised when selector configuration or GitHub JSON is invalid."""


class EvidenceSelectionError(RuntimeError):
    """Raised when exact evidence is missing or ambiguous."""


@dataclass(frozen=True)
class RequiredWorkflow:
    name: str
    database_id: int
    event: str


@dataclass(frozen=True)
class WorkflowRun:
    attempt: int
    conclusion: str
    database_id: int
    event: str
    head_branch: str
    head_sha: str
    status: str
    url: str
    workflow_name: str
    workflow_database_id: int


@dataclass(frozen=True)
class Evidence:
    workflow: str
    workflow_database_id: int
    run_id: int
    attempt: int
    url: str


def validate_repository(value: str) -> str:
    owner, separator, repository = value.partition("/")
    if (
        separator != "/"
        or "/" in repository
        or OWNER_PATTERN.fullmatch(owner) is None
        or REPOSITORY_PATTERN.fullmatch(repository) is None
        or repository in {".", ".."}
        or repository.lower().endswith(".git")
    ):
        raise EvidenceInputError("repository is not canonical")
    return value


def validate_deploy_sha(value: str) -> str:
    if SHA_PATTERN.fullmatch(value) is None:
        raise EvidenceInputError("deploy SHA is not canonical")
    return value


def parse_required_workflows(values: Sequence[str]) -> list[RequiredWorkflow]:
    if not values:
        raise EvidenceInputError("at least one workflow is required")

    workflows: list[RequiredWorkflow] = []
    names: set[str] = set()
    database_ids: set[int] = set()
    for value in values:
        if value.count("=") != 1:
            raise EvidenceInputError("workflow identity is invalid")
        name, raw_identity = value.split("=", maxsplit=1)
        if raw_identity.count("@") != 1:
            raise EvidenceInputError("workflow identity is invalid")
        raw_database_id, event = raw_identity.split("@", maxsplit=1)
        if (
            not name
            or name != name.strip()
            or any(ord(character) < 0x20 for character in name)
            or POSITIVE_INTEGER_PATTERN.fullmatch(raw_database_id) is None
            or EVENT_PATTERN.fullmatch(event) is None
        ):
            raise EvidenceInputError("workflow identity is invalid")
        database_id = int(raw_database_id)
        if name in names or database_id in database_ids:
            raise EvidenceInputError("workflow identities must be unique")
        names.add(name)
        database_ids.add(database_id)
        workflows.append(
            RequiredWorkflow(name=name, database_id=database_id, event=event)
        )
    if {workflow.name: workflow.event for workflow in workflows} != (
        REQUIRED_WORKFLOW_EVENTS
    ):
        raise EvidenceInputError("release workflow policy is invalid")
    return workflows


def _required_string(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise EvidenceInputError("workflow run schema is invalid")
    return value


def _required_positive_integer(record: dict[str, object], field: str) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EvidenceInputError("workflow run schema is invalid")
    return value


def parse_runs(payload: object) -> list[WorkflowRun]:
    if not isinstance(payload, list):
        raise EvidenceInputError("GitHub run input must be an array")
    if len(payload) >= GITHUB_FILTERED_RESULT_CAP:
        raise EvidenceInputError("GitHub run input reached the truncation sentinel")

    runs: list[WorkflowRun] = []
    for item in payload:
        if not isinstance(item, dict):
            raise EvidenceInputError("workflow run schema is invalid")
        runs.append(
            WorkflowRun(
                attempt=_required_positive_integer(item, "attempt"),
                conclusion=_required_string(item, "conclusion"),
                database_id=_required_positive_integer(item, "databaseId"),
                event=_required_string(item, "event"),
                head_branch=_required_string(item, "headBranch"),
                head_sha=_required_string(item, "headSha"),
                status=_required_string(item, "status"),
                url=_required_string(item, "url"),
                workflow_name=_required_string(item, "workflowName"),
                workflow_database_id=_required_positive_integer(
                    item, "workflowDatabaseId"
                ),
            )
        )
    return runs


def select_evidence(
    *,
    repository: str,
    deploy_sha: str,
    required_workflows: Sequence[RequiredWorkflow],
    runs: Sequence[WorkflowRun],
) -> list[Evidence]:
    selected: list[Evidence] = []
    for required in required_workflows:
        matches = [
            run
            for run in runs
            if run.workflow_name == required.name
            and run.workflow_database_id == required.database_id
            and run.event == required.event
            and run.head_branch == "main"
            and run.head_sha == deploy_sha
            and run.status == "completed"
            and run.conclusion == "success"
            and run.url
            == f"https://github.com/{repository}/actions/runs/{run.database_id}"
        ]
        if len(matches) != 1:
            raise EvidenceSelectionError(
                "required workflow evidence is missing or ambiguous"
            )
        run = matches[0]
        selected.append(
            Evidence(
                workflow=required.name,
                workflow_database_id=required.database_id,
                run_id=run.database_id,
                attempt=run.attempt,
                url=f"{run.url}/attempts/{run.attempt}",
            )
        )
    return selected


def _reject_json_constant(_: str) -> None:
    raise EvidenceInputError("JSON input contains a non-standard number")


def read_json(path: str, standard_input: TextIO) -> object:
    if path == "-":
        return json.load(standard_input, parse_constant=_reject_json_constant)
    with Path(path).open(encoding="utf-8") as input_file:
        return json.load(input_file, parse_constant=_reject_json_constant)


def parse_arguments(arguments: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select exact-SHA GitHub Actions release evidence from gh run list JSON."
        )
    )
    parser.add_argument("--repository", required=True, metavar="OWNER/REPO")
    parser.add_argument("--deploy-sha", required=True, metavar="SHA")
    parser.add_argument(
        "--require-workflow",
        required=True,
        action="append",
        metavar="LABEL=WORKFLOW_DATABASE_ID@EVENT",
    )
    parser.add_argument(
        "--input",
        default="-",
        metavar="PATH",
        help="gh run list JSON array path, or - for standard input",
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    options = parse_arguments(sys.argv[1:] if arguments is None else arguments)
    try:
        repository = validate_repository(options.repository)
        deploy_sha = validate_deploy_sha(options.deploy_sha)
        required_workflows = parse_required_workflows(options.require_workflow)
        runs = parse_runs(read_json(options.input, sys.stdin))
        evidence = select_evidence(
            repository=repository,
            deploy_sha=deploy_sha,
            required_workflows=required_workflows,
            runs=runs,
        )
    except EvidenceSelectionError:
        print(
            "release-evidence: FAIL (required evidence missing or ambiguous)",
            file=sys.stderr,
        )
        return 1
    except (EvidenceInputError, OSError, UnicodeError, json.JSONDecodeError):
        print("release-evidence: ERROR (invalid selector input)", file=sys.stderr)
        return 2

    output = {
        "deploy_sha": deploy_sha,
        "evidence": [
            {
                "workflow": item.workflow,
                "workflow_database_id": item.workflow_database_id,
                "run_id": item.run_id,
                "attempt": item.attempt,
                "url": item.url,
            }
            for item in evidence
        ],
    }
    json.dump(output, sys.stdout, ensure_ascii=True, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
