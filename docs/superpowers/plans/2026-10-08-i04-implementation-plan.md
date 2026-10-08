# I04 implementation plan: PostgreSQL proof and signed webhook vertical slice

Status: approved for execution

Source design: [`2026-10-07-signed-webhook-postgres-design.md`](../specs/2026-10-07-signed-webhook-postgres-design.md)

Target release: CommerceOps Desk v0.2.0

## 1. Execution rules

- Deliver three independently reviewable pull requests in order. PR2 starts from PR1's merged `main`; PR3 starts from PR2's merged `main`.
- Use a dedicated linked worktree for each implementation PR. Never implement in the primary `main` checkout.
- Follow red-green-refactor for every behavior: add the smallest meaningful failing test, run it and confirm the expected failure, add the minimum implementation, rerun to green, then refactor.
- Do not publish a deliberately failing commit. Each public commit is a green checkpoint whose history still shows test-before-implementation changes in the same diff.
- Repository functions execute SQL but never call `commit()` or `rollback()`. The application service owns the business transaction.
- Never make a test pass with sleeps, broad exception swallowing, unbounded retry, weaker assertions, disabled constraints, or a SQLite substitute for a PostgreSQL claim.
- Keep all data synthetic. Never add real provider names, customer fields, credentials, production URLs, or copied customer payloads.
- Update public claims only after the corresponding executable evidence is green on `main`.
- Push each green checkpoint to both the platform origin and GitHub, then verify local and both remote SHAs.
- Create candidate merge commits with the verified GitHub noreply author/committer identity and run the reachable-history scan before updating `main`; do not rely on a hosting service's account-email default.

## 2. Pull-request sequence

| PR | Branch | Outcome | Explicit exclusions |
|---|---|---|---|
| PR1 | `feat/i04-postgres-proof` | Real PostgreSQL 17 migration, transaction, concurrency, and container-readiness evidence | no `0005`, webhook, UI, or version bump |
| PR2 | `feat/i04-signed-webhook-ingress` | Demo-only HMAC ingress, inbox, and atomic `payment.failed` business effect | no signer UI, outbox, worker, real provider, or version bump |
| PR3 | `feat/i04-webhook-simulator-ui` | Manager simulator, case provenance, browser journey, docs, and `0.2.0` version | no asynchronous delivery or live commerce integration |

Each PR links Issue #3, records its exact verification commands, and waits for green PR and post-merge `main` checks before the next branch starts.

## 3. PR1 — live PostgreSQL 17 proof

### 3.1 Create the isolated branch and prove the baseline

Files changed: none.

Actions:

1. Create `.worktrees/feat-i04-postgres-proof` from the verified `main` SHA.
2. Reuse the repository `.venv` and frontend dependencies only after confirming their lock files match; otherwise run `make setup`.
3. Run the existing baseline:

   ```bash
   make backend-test
   make sqlite-migrate-test
   make project-test
   make lint
   ```

4. If the baseline fails, diagnose it before adding feature changes.

### 3.2 Build the disposable PostgreSQL database harness

Test-first files:

- `backend/postgres_tests/test_harness.py`

Implementation files:

- `backend/postgres_tests/__init__.py`
- `backend/postgres_tests/harness.py`
- `backend/postgres_tests/conftest.py`
- `backend/pyproject.toml`
- `.dockerignore`
- `Makefile`

Tests to add first:

- a temporary database exists only inside the harness context;
- cleanup runs when the test body raises;
- a non-PostgreSQL administrator URL fails closed;
- generated names match `commerce_ops_test_[0-9a-f]{32}` and no caller-provided identifier becomes SQL;
- missing `COMMERCE_OPS_POSTGRES_TEST_ADMIN_URL` fails instead of skipping;
- error text and pytest output never expose a database password.

Implementation contract:

- Parse only `COMMERCE_OPS_POSTGRES_TEST_ADMIN_URL`; never fall back to the runtime database setting.
- Require a PostgreSQL backend and connect to its administrative database with psycopg autocommit.
- Generate the database name internally with `uuid4().hex` and use psycopg identifier composition for `CREATE/DROP DATABASE`.
- Yield a password-preserving SQLAlchemy URL without printing it.
- Dispose every registered engine, terminate exact-name remaining connections, and drop only the generated database in `finally`.
- Keep existing SQLite fixtures unchanged. `postgres_tests/conftest.py` builds its own migrated `Settings`, application, frozen clock, token factory, and TestClient.
- Register the `container` pytest marker. Add `postgres_tests` to the mypy target but not to default pytest `testpaths`.
- Exclude `backend/postgres_tests` from the production image.
- Add `postgres-migration-test`, `postgres-integration-test`, `postgres-test`, and `postgres-container-test` Make targets. The first three never silently skip.

RED command:

```bash
cd backend && ../.venv/bin/python -m pytest postgres_tests/test_harness.py -q
```

GREEN command: the same command against a local PostgreSQL service.

Green commit:

```text
建立 PostgreSQL 临时数据库验证基座
```

### 3.3 Prove migrations and database constraints

Test-first file:

- `backend/postgres_tests/test_migrations.py`

Tests:

- fresh `alembic upgrade head` reaches `0004_order_case`;
- a second upgrade is a no-op;
- `alembic check` reports no drift;
- `0004_order_case -> 0003_bootstrap_idempotency -> 0004_order_case` round trip succeeds;
- tenant-composite foreign keys reject cross-organization order, assignee, note, and audit references;
- case rule-shape, lifecycle, resolution, version, and unique event constraints reject invalid rows;
- assertions inspect the applied revision, named constraints, and exact record counts, not only process exit codes.

Implementation should need only the harness unless a real cross-dialect defect appears. Any defect fix goes in its own test-backed commit and changes only the responsible migration/model code.

RED/GREEN command:

```bash
make postgres-migration-test
```

Green commit:

```text
验证 PostgreSQL 迁移与租户约束
```

### 3.4 Prove the existing runtime and named concurrency behavior

Test-first files:

- `backend/postgres_tests/test_workflow.py`
- `backend/postgres_tests/test_concurrency.py`

Runtime test:

- `/ready` reports the PostgreSQL dependency reachable;
- bootstrap a Manager workspace;
- assign `DEMO-1043`;
- switch to Agent;
- add a note and resolve the case;
- assert final case version, one command receipt per command, and ordered audit events directly in PostgreSQL.

PostgreSQL-specific tests:

- rate-limit upsert persists its bounded count;
- the global capacity lock prevents overshoot;
- concurrent bootstrap exact retries create one workspace and replay one result;
- concurrent exact case-command retries create one effect;
- same idempotency key with different payload yields one success and one stable conflict;
- two different keys updating the same case version yield one winner and one version conflict;
- concurrent reset yields one replacement workspace and one loser response without partial data.

Synchronization rules:

- Every worker owns its own Session/TestClient.
- Use `threading.Barrier` or a narrowly injected SQLAlchemy hook; never use `sleep`.
- Query final state with a fresh Session.
- Exercise the actual PostgreSQL URL and assert `engine.dialect.name == "postgresql"`.

Potential implementation-fix files, only when a failing test proves the need:

- `backend/app/services/bootstrap_idempotency.py`
- `backend/app/services/demo_workspaces.py`
- `backend/app/services/case_commands.py`
- `backend/app/api/demo.py`

Do not classify an arbitrary `IntegrityError` as replay. Roll back an aborted PostgreSQL transaction before reading a committed winner.

RED/GREEN command:

```bash
make postgres-integration-test
```

Green commit:

```text
验证 PostgreSQL 事务与并发语义
```

### 3.5 Prove the production container's database readiness

Test-first files:

- `backend/postgres_tests/test_container.py`
- `scripts/tests/test_container_packaging.py`

Implementation files:

- `Dockerfile`
- `.dockerignore`
- `Makefile`

Tests and implementation:

- Build one named image supplied through `COMMERCE_OPS_CONTAINER_IMAGE`.
- Start it without a repository mount, Docker socket, `--privileged`, or root user.
- Use a read-only root filesystem plus the minimum `/app/data` temporary mount.
- Connect to runner PostgreSQL through `host.docker.internal:host-gateway`.
- Map only `127.0.0.1:<ephemeral-port>:8000`.
- Run with production environment, explicit non-production session secret, and an isolated temporary database.
- Change the Docker healthcheck to read `PORT` and probe `/ready`, not the fixed-port `/health` endpoint.
- Wait for Docker health plus `GET /ready == {"status":"ready","database":"reachable"}`.
- Confirm startup applied `0004_order_case` to the target database.
- Always remove the container and redact DSNs from failure output.
- Assert neither `backend/tests` nor `backend/postgres_tests` is copied into the runtime image.

RED/GREEN commands:

```bash
docker build -t commerce-ops-desk:postgres-ci .
COMMERCE_OPS_CONTAINER_IMAGE=commerce-ops-desk:postgres-ci make postgres-container-test
```

Green commit:

```text
增加 PostgreSQL 容器就绪验证
```

### 3.6 Add the public GitHub Actions gate

Test-first files:

- `scripts/tests/test_project_tooling.py`

Implementation file:

- `.github/workflows/verify.yml`

Contract tests require:

- the original SQLite/browser job remains;
- a parallel `PostgreSQL 17 integration` job exists;
- `postgres:17-alpine` is pinned to a registry-verified immutable SHA-256 digest;
- service health uses `pg_isready`;
- checkout uses the exact PR head SHA, never the synthetic merge ref;
- only locked Python dependencies are installed;
- migration, integration, Docker build, and container targets all run;
- workflow permissions remain `contents: read` only;
- there is no `pull_request_target`, `secrets.*`, image push, database artifact upload, production token, or write permission.

Before editing the workflow, resolve the real image digest with Docker/registry metadata and record the immutable value. Do not guess it.

Because the primary GitHub credential lacks `workflow` scope, use a separately authorized credential with `repo` and `workflow` only for the workflow commit push. If a temporary collaborator is required, add it immediately before the push and remove it after merge. Never print or persist either token in the repository.

RED command:

```bash
.venv/bin/python -m pytest scripts/tests/test_project_tooling.py -q
```

GREEN commands:

```bash
make project-test
make postgres-test
```

Green commit:

```text
接入 PostgreSQL 公共 CI 闸门
```

### 3.7 Publish only the newly proved boundary

Test-first file:

- `scripts/tests/test_project_tooling.py`

Documentation files:

- `README.md`
- `docs/design-summary.md`
- `docs/security-model.md`

Replace “offline DDL only” with the precise statement that PostgreSQL 17 fresh/repeat migrations, selected constraints, the I03 workflow, named concurrency behavior, and production-container database readiness run in public CI. Continue to state that:

- coverage is selected, not universal;
- multi-node migration coordination, HA, backups, disaster recovery, and performance are unproved;
- signed webhook intake and asynchronous processing are not implemented yet;
- the application remains `0.1.0`.

Green commit:

```text
更新 PostgreSQL 已验证边界
```

### 3.8 PR1 final gate

Run against the pinned local PostgreSQL image:

```bash
make postgres-migration-test
make postgres-integration-test
docker build -t commerce-ops-desk:postgres-ci .
COMMERCE_OPS_CONTAINER_IMAGE=commerce-ops-desk:postgres-ci make postgres-container-test
make verify
git diff --check
```

Then:

1. scan the worktree and reachable history;
2. push and verify both remotes;
3. open the PR with the exact local evidence;
4. wait for both public jobs;
5. merge only when every required check is green;
6. wait for post-merge `main` checks;
7. remove the feature branch and any temporary collaborator;
8. update Issue #3 with links and the exact behaviors now proved.

## 4. PR2 — signed webhook ingress and atomic business effect

### 4.1 Configuration and secret loading

Test-first files:

- `backend/tests/test_settings.py`
- `scripts/tests/test_start_hosted_webhook_secret.py`

Implementation files:

- `backend/app/config.py`
- `scripts/start-hosted.sh`
- `.env.example`

Add `webhook_enabled=false`, `webhook_master_secret`, `webhook_source_minute_limit=120`, and `demo_webhook_event_limit=20` to Settings. `start-hosted.sh` defaults the feature on only when `COMMERCE_OPS_ENVIRONMENT=demo`; production leaves it off unless explicitly enabled. An enabled demo atomically creates and reuses `data/.webhook-secret`; production never creates one and fails closed without an explicit source. The direct environment value takes precedence over a file. Validate the exact one-LF/CRLF rule, UTF-8, byte length, ownership, mode 0600, symlink rejection, concurrent first start, and complete stdout/stderr redaction.

Green commit:

```text
建立 webhook 密钥配置边界
```

### 4.2 Migration and integration lifecycle

Test-first files:

- `backend/tests/test_migrations.py`
- `backend/postgres_tests/test_migrations.py`
- `backend/tests/test_webhook_integrations.py`
- existing bootstrap/reset tests

Implementation files:

- `backend/alembic/versions/0005_webhook_inbox.py`
- `backend/app/models/webhook_integration.py`
- `backend/app/models/webhook_event.py`
- `backend/app/models/organization.py`
- `backend/app/models/__init__.py`
- `backend/app/repositories/webhook_integrations.py`
- `backend/app/services/demo_workspaces.py`

Create the two tables and organization counter exactly as designed. Backfill one integration per existing demo organization using Python `uuid4`, never a database-specific UUID function. Prove SQLite and PostgreSQL fresh migration, existing-data upgrade, repeat upgrade, `head -> 0004 -> head`, downgrade preservation of pre-I04 records, constraints, indexes, composite foreign keys, and cascade cleanup. New workspace creation and reset create one distinct integration in the same transaction; non-demo creation fails.

Green commits:

```text
增加 webhook 收件箱数据契约
绑定演示工作区 integration 生命周期
```

### 4.3 Pure signing and strict payload parsing

Test-first files:

- `backend/tests/test_webhook_auth.py`
- `backend/tests/test_webhook_payload.py`

Implementation files:

- `backend/app/auth/webhook.py`
- `backend/app/domain/webhook_event.py`

Use fixed, independently calculated test vectors for key derivation and signatures. Cover canonical raw-path UUID, percent-encoding rejection, duplicate raw headers, timestamp/event/signature grammar, byte-level whitespace changes, `compare_digest`, and inclusive ±300-second limits. Parse strict UTF-8 JSON after authentication; reject BOM, duplicate keys, non-standard numbers, trailing data, non-object roots, bool-as-int, unexpected fields, live-looking identifiers, unsupported media type, and content encoding.

Green commit:

```text
实现 webhook 签名与严格负载解析
```

### 4.4 Durable pre-authentication source limiter

Test-first files:

- `backend/tests/test_webhook_rate_limits.py`
- `backend/postgres_tests/test_webhook_rate_limits.py`
- existing demo rate-limit tests

Implementation files:

- `backend/app/repositories/rate_limits.py`
- `backend/app/services/webhook_ingress.py`
- `backend/app/services/demo_workspaces.py`

Extract the current dialect-specific fixed-window upsert without changing existing demo semantics. Store only a domain-separated keyed source digest. The API uses a dedicated short Session to increment and commit this counter before integration lookup or authentication; later 401/415/422/business rollback must not undo it. Prove the minute boundary, limit race, raw-address absence, trusted-proxy handling, and `Retry-After`.

Green commit:

```text
增加 webhook 入口持久限流
```

### 4.5 Inbox/order primitives and the one-commit processor

Test-first files:

- `backend/tests/test_webhook_repository.py`
- `backend/postgres_tests/test_webhook_repository.py`
- `backend/tests/test_webhook_processing.py`
- SQLite and PostgreSQL webhook concurrency tests

Implementation files:

- `backend/app/repositories/webhook_events.py`
- `backend/app/repositories/webhook_orders.py`
- `backend/app/services/webhook_processing.py`

Use dialect-native inserts with explicit conflict targets. Repositories never commit. The processor owns one transaction:

1. claim the event;
2. replay the committed winner or reject a digest conflict;
3. consume one demo event allowance;
4. conflict-safely create/read and validate the order;
5. set `payment_status=failed`;
6. create the open version-1 case with the existing two-hour SLA;
7. append `case.created_from_webhook` with a null actor;
8. complete inbox references;
9. commit once.

Prove exact replay, changed bytes, two tenants with one event ID, two events sharing a new order, allowance exhaustion, immutable snapshot conflict, injected failure at every stage, winner rollback/retry, and absence of raw body/signature/header/secret columns. Each thread owns its Session; barriers/hooks replace sleeps. Because the organization counter serializes same-tenant processors, add a lower-level PostgreSQL two-Session order-upsert race so the order conflict path is genuinely exercised.

Green commits:

```text
实现 webhook 幂等存储原语
完成 webhook 单事务业务闭环
验证 webhook PostgreSQL 并发语义
```

### 4.6 HTTP API, errors, logs, and public contract

Test-first files:

- `backend/tests/test_webhook_api.py`
- `backend/tests/test_webhook_target_auth.py`
- `backend/tests/test_webhook_transactions.py`
- `backend/tests/test_request_guard.py`
- PostgreSQL equivalents for concurrency and fault injection

Implementation files:

- `backend/app/api/webhooks.py`
- `backend/app/main.py`
- `backend/app/repositories/audit.py`
- `README.md`
- `docs/security-model.md`
- `docs/design-summary.md`

Register ingress only when enabled with a valid secret. Use a raw string path and ASGI raw path/header data. Join integration and organization; malformed, unknown, disabled, non-demo, and expired targets perform dummy HMAC work and share `401 webhook_authentication_failed`. Implement the remaining fixed 413/415/422/409/429/503 response codes from the design. A result-unknown commit failure is recoverable by retrying the same event ID/body with a fresh timestamp/signature.

Leak tests seed unique markers into every secret, signature, body, header, and source field, then assert the response, `caplog`, stdout, and stderr contain none. Expected 4xx paths do not log stacks; unknown errors log only request ID, stable phase/code, and exception class name without SQL parameters.

PR2 final gate:

```bash
make backend-test
make sqlite-migrate-test
make postgres-test
make project-test
make lint
git diff --check
```

PR2 remains version `0.1.0` and makes no real-provider or asynchronous claim.

## 5. PR3 — visible simulator, provenance, and browser evidence

### 5.1 Manager-only signed-envelope endpoint

Test-first file:

- `backend/tests/test_webhook_simulator.py`

Implementation files:

- `backend/app/api/webhook_demo.py`
- `backend/app/services/webhook_simulator.py`
- `backend/app/main.py`

Add `POST /api/demo/webhooks/envelope` with only `{"scenario":"fresh"|"stale"}`. Require demo mode, enabled webhook, Manager auth, same origin, and CSRF. The server owns every signed field and uses current time or exactly minus 301 seconds. Return `Cache-Control: no-store`; never return or log a key. Agent receives 403; disabled/production route is 404.

Green commit:

```text
增加服务端合成事件签名器
```

### 5.2 Safe case provenance

Test-first files:

- `backend/tests/test_case_api.py`
- `backend/tests/test_case_permissions.py`

Implementation files:

- `backend/app/repositories/cases.py`
- `backend/app/services/case_queries.py`
- `backend/app/api/cases.py`

Add a tenant-safe outer join and expose only source kind, provider, event type, constrained external event ID, and received time. Seed cases report `seeded_demo`; webhook cases report `synthetic_webhook`. Never expose integration ID, digest, signature, headers, or body. Existing Agent assignment visibility still governs the whole record.

Green commit:

```text
展示案件安全来源信息
```

### 5.3 Frontend client and isolated panel state machine

Test-first files:

- `frontend/src/api/webhooks.test.ts`
- `frontend/src/features/webhooks/SyntheticProviderPanel.test.tsx`

Implementation files:

- `frontend/src/api/webhooks.ts`
- `frontend/src/features/webhooks/SyntheticProviderPanel.tsx`

The signer call uses cookie and CSRF; webhook delivery uses `credentials: "omit"`. The envelope lives only in React memory and never enters storage, URL, error objects, logs, or rendered DOM. Fresh delivery sends the returned bytes unchanged. Replay uses the fresh cached envelope. Tamper changes one body byte after signing. Stale uses a server-generated stale envelope. Model loading, success, replay, authentication rejection, validation error, both 429 cases, network failure, and committed-but-refresh-failed separately; never automatically resend after a committed delivery.

Green commit:

```text
实现合成 Provider 面板状态机
```

### 5.4 Integrate the Manager workspace and responsive provenance UI

Test-first file:

- `frontend/src/features/demo/DemoWorkspace.i04.test.tsx`

Implementation files:

- `frontend/src/api/operations.ts`
- `frontend/src/features/demo/DemoWorkspace.tsx`
- `frontend/src/app/App.css`

Only Managers see the panel. A successful new event refreshes dashboard/queue and opens the returned case. Queue/detail show `Synthetic webhook` or `Seeded demo data`; detail renders only safe provenance. Add `aria-live` feedback, scoped busy states, keyboard access, touch-size controls, and no horizontal overflow at 320 px.

Green commit:

```text
接入 Manager 事件到案件演示
```

### 5.5 Browser evidence, feedback path, docs, and version

Test-first/contract files:

- `frontend/e2e/i04-webhook.spec.ts`
- `frontend/e2e/fixtures.ts`
- `scripts/tests/test_project_tooling.py`
- version assertions in backend/frontend tests

Documentation and metadata:

- `README.md`
- `docs/security-model.md`
- `docs/design-summary.md`
- `.github/ISSUE_TEMPLATE/engineering-feedback.yml`
- `backend/pyproject.toml`
- `backend/app/main.py`
- `frontend/package.json`
- `frontend/package-lock.json`

Browser journeys prove:

- new delivery changes dashboard/queue/detail/audit;
- replay returns the same case and does not change counts;
- tamper and stale deliveries produce only the two explicitly expected 401 responses and no data change;
- Manager assigns, Agent notes, and Agent resolves the webhook-created case;
- desktop, mobile, and 320 px layouts remain usable.

Do not globally ignore console, page, or HTTP errors. Register only the two scenario-specific expected 401 responses. Add a structured engineering-feedback issue form asking for reproduction, environment, expected/actual behavior, and optional implementation interest; do not claim this constitutes external feedback.

Only after every final gate passes, update all synchronized application versions to `0.2.0`.

Green commits:

```text
验证签名事件浏览器闭环
补齐 v0.2 反馈入口与发布说明
```

PR3 final gate:

```bash
make verify
make postgres-test
docker build -t commerce-ops-desk:0.2.0 .
COMMERCE_OPS_CONTAINER_IMAGE=commerce-ops-desk:0.2.0 make postgres-container-test
git diff --check
```

## 6. v0.2.0 release and portfolio evidence

After PR3 and post-merge `main` CI are green:

1. Re-run the complete release gate from a clean checkout and record exact counts and duration.
2. Deploy the verified SHA to the existing access-controlled workstation environment and verify `/health`, `/ready`, and the full demo journey. Do not call it an anonymous public demo while the access-code boundary remains.
3. Record a real browser walkthrough showing fresh delivery, exact replay, tamper rejection, stale rejection, assignment, Agent note, resolution, provenance, and audit history.
4. Verify the video contains only synthetic data and no signature, secret, raw body, token, access code, browser extension, or unrelated personal information.
5. Tag the verified `main` SHA as `v0.2.0`; upload the video, architecture diagram, and checksums; verify anonymous release-asset access.
6. Update the repository and profile README with exact CI/release links and truthful claims.
7. Keep CommerceOps Desk first in the profile README. The account owner still needs to place it first in GitHub's manual **Customize your pins** UI because GitHub exposes no supported public mutation for profile pins.
8. Prepare a concise X thread and one personalized recruiter/client reply linking the repository and release video. State that the provider and data are synthetic and invite review of the HMAC, idempotency, transaction, or PostgreSQL implementation.
9. Ask for one concrete external action: reproduce a scenario, open the engineering-feedback form, review a named PR, or request a scoped adaptation. Stars alone do not count as technical feedback.
10. Record only genuine third-party comments, issues, pull requests, or project inquiries. Never manufacture engagement with the secondary account.

## 7. Completion evidence

The v0.2 engineering objective is proved only when:

- design PR and all three implementation PRs are merged with green public checks;
- local, platform-origin, GitHub `main`, release tag, and deployed SHA agree where they are expected to agree;
- SQLite, PostgreSQL, container, frontend, and browser gates cover the named design requirements without skip or substitution;
- the release assets are anonymously accessible and checksummed;
- public docs and X copy make every synthetic and unproved boundary explicit;
- the profile exposes the flagship repository and a direct contact path;
- at least one real external person provides technical feedback or a scoped project inquiry.

Until the final external condition is observed, the broader portfolio goal remains active even if v0.2.0 itself is shipped.
