# CommerceOps Desk v0.2.1 evaluator release implementation plan

Status: approved for execution

Source design: [`2026-10-10-evaluator-release-design.md`](../specs/2026-10-10-evaluator-release-design.md)

Implementation branch: `feat/evaluator-release-implementation`

Verified base: `215fb4088fa3db7a66d812507fb49a4190f08a07`

## 1. Execution rules

- Work only in the dedicated implementation worktree created from the verified
  remote `main`. The approved design commit is the only commit carried from the
  earlier design branch.
- Use red-green-refactor for every behavior. Add the smallest meaningful test,
  run it, and confirm it fails for the intended missing behavior before changing
  implementation code.
- Keep public commits green. RED evidence is observed locally, not pushed as a
  broken checkpoint.
- The public build endpoint is fixed as `GET /api/build` with exactly
  `service`, `version`, and `source_sha`. Do not expose configuration or runtime
  inspection data.
- `source_sha: null` is valid only for local/source-hosted builds. The verified
  image and workstation candidate require one exact lowercase 40-character SHA.
- Never put `COMMERCE_OPS_SOURCE_SHA` in Compose runtime overrides. It is baked
  into the immutable image from the same verified build argument as the OCI
  revision label.
- Do not strengthen the new-candidate identity rule in a way that prevents the
  frozen v0.2.0 legacy container from being a valid rollback target.
- The evaluator guide is instructional only. It never tracks progress, performs
  actions, persists state, or claims that free-text data is technically scanned.
- All test notes use clearly fictional text. No real personal, customer,
  credential, access-code, or confidential data enters code, fixtures, logs, or
  artifacts.
- Push every green checkpoint to both configured remotes and verify that both
  remote branch SHAs equal local `HEAD`.
- Merge the implementation pull request only through a locally created commit
  whose author and committer both use the verified GitHub noreply identity. Do
  not use GitHub's server-generated merge commit path.

## 2. Checkpoint sequence

| Checkpoint | Outcome | Green commit |
|---|---|---|
| P0 | Approved design plus executable implementation plan | `规划在线评估版本实施步骤` |
| P1 | Public backend build-identity contract | `增加公开构建身份契约` |
| P2 | Verified image and workstation bind runtime SHA to source | `绑定镜像与部署源码身份` |
| P3 | Shared evaluator guide and safe free-text guidance | `增加五步评估指引` |
| P4 | Non-blocking frontend build provenance | `展示运行版本与完整源码提交` |
| P5 | Desktop/mobile browser proof and runbook update | `验证评估版桌面与移动流程` |
| P6 | Release-facing documentation without premature live-demo promotion | `准备评估版本发布证据` |
| P7 | Release-gate audit closeout | `补齐评估发布安全门禁` |

The exact deployment hostname already exists in versioned engineering files
and public Git history. This checkpoint does not add or promote a live-demo CTA,
and it does not claim that the evaluator is deployed, released, or approved for
external access. README/profile promotion remains a post-deployment step after
external acceptance and the post-release provenance gate.

## 3. P0 — baseline and plan

Files changed:

- `docs/superpowers/specs/2026-10-10-evaluator-release-design.md`
- `docs/superpowers/plans/2026-10-10-evaluator-release-implementation-plan.md`

Actions:

1. Confirm both remote `main` refs equal the verified base.
2. Reuse the locked Python environment only after dependency lock files match.
3. Install this worktree's own frontend dependencies; never symlink
   `frontend/node_modules` across worktrees because TypeScript may emit absolute
   private type paths.
4. Run `make lint` and `make public-scan` before feature edits.
5. Commit and push the approved design and this plan as a green checkpoint.

## 4. P1 — backend build identity

Test-first files:

- `backend/tests/test_settings.py`
- `backend/tests/test_health.py`
- `scripts/tests/test_project_tooling.py`

Implementation files:

- `backend/app/version.py` (new)
- `backend/app/config.py`
- `backend/app/api/health.py`
- `backend/app/main.py`

### RED behavior

- `Settings().source_sha` defaults to `None`.
- `COMMERCE_OPS_SOURCE_SHA` accepts one lowercase 40-character hex SHA.
- Empty, abbreviated, long, uppercase, whitespace-padded, and non-hex values
  fail closed without normalization.
- `GET /api/build` is unauthenticated and returns exactly:

  ```json
  {
    "service": "commerce-ops-desk",
    "version": "0.2.1",
    "source_sha": null
  }
  ```

- With configured identity, the endpoint returns the full SHA and
  `Cache-Control: no-store`.
- The FastAPI version, build response, backend package, frontend package, and
  lock-file version remain tied to one `APP_VERSION` constant.
- Hardened demo mode keeps `/api/build` available while `/docs`, `/redoc`, and
  `/openapi.json` remain unavailable.

RED command:

```bash
cd backend
../.venv/bin/python -m pytest -q tests/test_settings.py tests/test_health.py
```

Implementation:

- Define `APP_VERSION = "0.2.1"` and `SERVICE_ID = "commerce-ops-desk"` in the
  new version module.
- Add an optional, strictly validated `source_sha` setting.
- Add a typed build payload and route to the existing system router before the
  unknown `/api/*` catch-all.
- Read identity from `request.app.state.settings`; never read the environment
  again in the request handler.
- Set `Cache-Control: no-store` explicitly and return no additional field.
- Replace the FastAPI version literal with `APP_VERSION` and update the tooling
  contract to verify the constant instead of using a regex tied to `main.py`.

GREEN commands:

```bash
cd backend
../.venv/bin/python -m pytest -q tests/test_settings.py tests/test_health.py
cd ..
.venv/bin/python -m pytest -q scripts/tests/test_project_tooling.py
make lint
```

## 5. P2 — verified image and workstation source binding

Test-first files:

- `scripts/tests/test_container_packaging.py`
- `scripts/tests/test_verified_image_build.py`
- `scripts/tests/test_workstation_runtime_contract.py`
- `scripts/tests/test_workstation_deployment.py`
- `backend/postgres_tests/test_container.py`

Implementation files:

- `Dockerfile`
- `deploy/workstation/build_verified_image.py`
- `deploy/workstation/deploy.py`

### RED behavior

- The runtime image contains exactly one
  `COMMERCE_OPS_SOURCE_SHA=<SOURCE_SHA>` inherited from its immutable image
  configuration.
- The Docker build rejects any build argument that is not a full lowercase
  40-character SHA.
- The verified-image builder rejects missing, duplicate, malformed, or
  mismatched runtime SHA even when the OCI revision label is correct.
- New-candidate preflight rejects both of these cases:
  - image and container omit the runtime source SHA;
  - image and container agree with each other but disagree with the candidate
    identity while the OCI label remains correct.
- The live PostgreSQL container proof reads `/api/build` and observes the exact
  source SHA plus `no-store`; it does not inject a runtime override.

Targeted RED commands:

```bash
.venv/bin/python -m pytest -q \
  scripts/tests/test_container_packaging.py \
  scripts/tests/test_verified_image_build.py
```

```bash
.venv/bin/python -m pytest -q \
  scripts/tests/test_workstation_runtime_contract.py \
  scripts/tests/test_workstation_deployment.py
```

Implementation:

- Validate `SOURCE_SHA` in the runtime Docker stage, retain the OCI revision
  label, and set `ENV COMMERCE_OPS_SOURCE_SHA=$SOURCE_SHA` from the same build
  argument.
- Parse the immutable image `Config.Env` without echoing its content. Bind the
  one runtime SHA to the builder's approved source and keep build-manifest
  schema 2 unchanged.
- Reuse a strict environment parser in workstation candidate validation. Bind
  the immutable image baseline and created container to
  `CandidateIdentity.source_sha` in addition to the existing exact environment
  comparison.
- Apply the stronger check only to new candidate/image preflight. Do not require
  a source environment variable from the frozen legacy rollback profile.

GREEN commands:

```bash
.venv/bin/python -m pytest -q \
  scripts/tests/test_container_packaging.py \
  scripts/tests/test_verified_image_build.py \
  scripts/tests/test_workstation_runtime_contract.py \
  scripts/tests/test_workstation_deployment.py
```

```bash
.venv/bin/python -m pytest -q scripts/tests/test_workstation*.py
```

The real image/PostgreSQL container proof remains a GitHub CI gate after local
contract tests pass.

## 6. P3 — shared evaluator guide and fictional-text boundary

Test-first files:

- `frontend/src/features/demo/EvaluatorGuide.test.tsx` (new)
- `frontend/src/app/App.test.tsx`
- `frontend/src/features/demo/DemoWorkspace.i03.test.tsx`

Implementation files:

- `frontend/src/features/demo/EvaluatorGuide.tsx` (new)
- `frontend/src/features/demo/DemoEntry.tsx`
- `frontend/src/features/demo/DemoWorkspace.tsx`
- `frontend/src/app/App.css`

### RED behavior

- Entry and workspace render one shared ordered list with exactly the approved
  five steps: create, boundary, assign, work, verify.
- The guide contains no progress checkbox, completion state, automatic action,
  local storage, or extra server write.
- Manager and Agent workspaces both show the compact guide; its first step makes
  the Manager requirement understandable.
- Entry and note form show the exact static rule:
  `Use fictional text only. Do not enter personal, customer, credential, or
  confidential data.`
- The note textarea has that stable helper as its accessible description during
  normal, saving, recovery, and error states.

RED command:

```bash
(
  cd frontend
  npm exec -- vitest run \
    src/features/demo/EvaluatorGuide.test.tsx \
    src/features/demo/DemoWorkspace.i03.test.tsx \
    src/app/App.test.tsx
)
```

Implementation:

- Keep the five-step data in one component and expose only entry/compact visual
  variants.
- Use semantic `aside`, heading, and ordered-list markup.
- Replace the obsolete four-step I03 entry copy.
- Render compact guidance outside operational loading/error branches so it
  remains available when data loading fails.
- Associate a persistent helper ID with the note textarea through
  `aria-describedby`. Do not use `role="alert"` and do not add content scanning.
- Keep the entry CTA visible and use one-column behavior at narrow viewports;
  the complete SHA added later must wrap without horizontal overflow.

GREEN command: the same targeted Vitest command, followed by
`npm --prefix frontend run typecheck`.

## 7. P4 — frontend provenance without blocking the product

Test-first files:

- `frontend/src/api/build.test.ts` (new)
- `frontend/src/app/App.test.tsx`
- affected fetch mocks in workspace component tests

Implementation files:

- `frontend/src/api/build.ts` (new)
- `frontend/src/app/App.tsx`
- `frontend/src/app/App.css`

### RED behavior

- The client accepts only the fixed service, version `0.2.1`, no extra keys,
  and either a full lowercase SHA or `null`.
- The request uses `cache: "no-store"` and same-origin credentials.
- Network, unreadable, unexpected, uppercase, abbreviated, or extra-field
  responses do not retain attacker-controlled response text.
- Metadata loads independently of session recovery and never blocks Manager or
  Agent entry.
- Full identity renders `v0.2.1`, the complete SHA, and a commit link built from
  the fixed public repository origin.
- `null` renders `Unverified local build` without a commit link; failure renders
  `Build identity unavailable` while the operational UI remains usable.
- Footer retains source and engineering links and labels the public walkthrough
  accurately as v0.2.0.

RED command:

```bash
(
  cd frontend
  npm exec -- vitest run \
    src/api/build.test.ts \
    src/app/App.test.tsx
)
```

Implementation must route fetch mocks by pathname because build metadata and
session recovery run concurrently; tests must not rely on response-call order.

GREEN command: the same targeted Vitest command, followed by all frontend unit
tests and type checking.

## 8. P5 — browser evidence and deployment runbook

Test-first files:

- `frontend/e2e/foundation.spec.ts`
- `frontend/e2e/i03-workflow.spec.ts`
- `frontend/e2e/i03-workspace.spec.ts`
- `frontend/e2e/readme-screenshots.spec.ts`
- `scripts/tests/test_workstation_deployment.py`

Implementation/configuration files:

- `frontend/playwright.config.ts`
- `deploy/workstation/RUNBOOK.md`
- frontend styles/components only when a real browser failure proves a need

### Browser contract

- The hosted test process receives one fixed legal SHA so the real footer path
  is exercised.
- Entry has the five ordered steps and fictional-text warning.
- The real footer exposes exact version/SHA, fixed commit URL, and no horizontal
  overflow.
- Before note entry, the browser verifies that the warning is visible and is
  the textarea's accessible description. The test enters clearly fictional
  text.
- Desktop and mobile Chromium exercise the guide, footer, note warning, Manager
  webhook flow, assignment, Agent note/resolution, provenance, and audit trail.
- Existing JS exception, console error, and unexpected 4xx/5xx guards remain
  active.

### Runbook contract

- Candidate loopback acceptance reads `/api/build` and requires version,
  `SOURCE_SHA`, and `Cache-Control: no-store` without changing Secure cookies or
  creating another route.
- External acceptance repeats the same build identity check after the route
  switch.
- The access code remains outside commands, repository data, logs, screenshots,
  and artifacts.

Targeted GREEN commands:

```bash
(
  cd frontend
  COMMERCE_OPS_VENV_DIR="$PWD/../.venv" npm exec -- \
    playwright test e2e/foundation.spec.ts e2e/i03-workspace.spec.ts \
    e2e/i03-workflow.spec.ts --project=desktop-chromium --workers=1
)
```

```bash
(
  cd frontend
  COMMERCE_OPS_VENV_DIR="$PWD/../.venv" npm exec -- \
    playwright test e2e/foundation.spec.ts e2e/i03-workspace.spec.ts \
    e2e/i03-workflow.spec.ts --project=mobile-chromium --workers=1
)
```

## 9. P6 — release-facing repository evidence

Files:

- `CHANGELOG.md`
- `README.md`
- `docs/design-summary.md`
- `docs/security-model.md`

Document only implemented, executable evidence:

- v0.2.1 build identity and five-step evaluator guidance;
- access-controlled evaluator boundary;
- `Live evaluator: single-node SQLite; PostgreSQL 17: CI-verified path only`;
- fictional free-text responsibility;
- exact exclusions already approved in the design.

Do not add or promote a live-demo CTA, claim a deployed instance, claim
production readiness, or create the GitHub release in this checkpoint.

## 10. P7 — release-gate audit closeout

Close every release-audit finding before opening the pull request:

- select GitHub Actions evidence only from exactly one completed successful
  Verify `push` run and one completed successful CodeQL Default Setup
  (`workflowName: CodeQL`) `dynamic` run; both must target `main`, match the
  expected workflow database ID, and report the full approved `DEPLOY_SHA`;
- require a fresh, empty candidate data directory and absent candidate state
  before any candidate-preparation Docker inspection or resource creation,
  while preserving interrupted state in quarantine instead of deleting or
  reusing it; publish the flushed state through a dirfd-bound
  `renameat2(RENAME_NOREPLACE)` so interruption cannot leave a two-hard-link
  state that the quarantine path refuses;
- pin both remote `main` refs to the explicitly approved `DEPLOY_SHA` and make
  staging fail immediately on any mismatch;
- enumerate the complete external-acceptance, access-code governance, and
  post-rollback smoke gates in the authoritative workstation runbook; and
- keep the evaluator guide aligned with visible control labels and prove the
  entry, exact-SHA link, Manager action, and Agent note/resolution path with
  real keyboard navigation on desktop and mobile.

The selector reads repository-external private JSON from the real `gh run list`
schema, including `attempt`. Required workflow arguments use
`LABEL=workflowDatabaseId@event`, specifically `Verify=$ID@push` and
`CodeQL=$ID@dynamic`. The selector strictly validates each input base run URL,
then emits only allowlisted evidence with the attempt and an attempt-specific
immutable `/actions/runs/<id>/attempts/<attempt>` URL. It never receives a
token, access code, or cookie through its arguments or output. The controller
requests 1,000 records while the selector accepts at most 999; reaching the
1,000-result GitHub API cap is treated as potentially truncated input and
fails closed rather than claiming global uniqueness from an incomplete list.

## 11. Pull-request and merge gate

Before opening the application pull request:

```bash
make verify
make public-scan
git diff --check
```

Push both remotes and require all GitHub PR checks green. Then create a local
merge commit using the configured noreply identity, run `make public-scan` on
that exact candidate merge, and push it only with an explicit lease against the
verified remote `main`. After merge, record the new full `DEPLOY_SHA` and wait
for one completed successful Verify `push` run and one completed successful
CodeQL Default Setup (`workflowName: CodeQL`) `dynamic` run. Both must target
`main`, use the expected workflow database ID, and report
`headSha == DEPLOY_SHA`.

No build, workstation prepare, route switch, tag, release, README live link,
repository homepage update, profile update, or Website-field update occurs
until those exact-main checks pass.

## 12. Deployment, release, and public entry points

After the application branch is merged and exact-main is green:

1. build `DEPLOY_SHA` only through `build_verified_image.py`;
2. prepare a fresh isolated candidate and validate loopback identity;
3. switch only the CommerceOps route through `deploy.py`;
4. complete layered unauthorized/authorized/Host, desktop/mobile, restart, log,
   and rollback-readiness acceptance;
5. obtain administrator confirmation for external access-code scope and
   revocation/rotation ownership;
6. tag and publish non-prerelease `v0.2.1` at exactly `DEPLOY_SHA`;
7. pass the post-release provenance gate; and
8. only then publish the live CTA in repository metadata and the profile while
   keeping the public release/video fallback.

Any failed or indeterminate deployment follows the four-way outcome handling in
the approved design and versioned runbook. No manual Caddy, ledger, or
`active.json` edit is an accepted recovery action.
