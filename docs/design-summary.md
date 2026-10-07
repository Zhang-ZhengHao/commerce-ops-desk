# CommerceOps Desk — Design Summary

## Current verified slice

I02 establishes the public demo identity and access boundary on top of the I01 hosted foundation:

- FastAPI serves the production React build and same-origin JSON API from an injected port, with liveness and database-readiness probes.
- Alembic upgrades SQLite through `0003_bootstrap_idempotency` before the hosted process accepts traffic, including databases that had already applied `0002_demo_identity`.
- Visitors can create a temporary Manager or Agent workspace without registration, recover the session after refresh, and switch between persisted demo identities.
- The server stores only keyed session-token hashes. Workspace creation is database-idempotent by source digest, command key, and canonical payload; exact retries replay one result and changed payloads return `409`.
- Role changes rotate the session and CSRF token, revoke the previous session, and support database-backed idempotent replay. Same-role requests preserve the session without database writes; real changes consume a persistent, concurrency-safe per-workspace quota.
- Organization IDs are carried through identity, membership, session, and command-receipt constraints. Shared guards distinguish insufficient role from an invisible cross-tenant resource.
- Origin-authority, CSRF, request-size, validation-response, source-rate, trusted-proxy, role-write, and global-capacity controls are covered by backend tests.
- Component tests and desktop, mobile, and 320-pixel browser coverage exercise the public entry and authenticated role flow while treating browser, console, and HTTP failures as test failures.
- Locked setup, hosted-start, offline runtime-recovery, and public-history scan contracts remain part of the release gate.

Exception records and operational workflows are deliberately absent from this slice. The interface exposes only behavior backed by the current API.

## Target product outcome (planned)

CommerceOps Desk is intended to give a small ecommerce operations team one place to inspect and resolve exceptions caused by payment failures, refund review, delayed fulfilment, and event-processing failures.

The primary demonstration will follow one complete path:

1. A server-signed synthetic webhook enters the external-integration route.
2. The API authenticates, validates, deduplicates, and persists the event with an outbox job in one transaction.
3. A worker applies deterministic rules and opens an exception case.
4. A manager assigns the case to an agent.
5. The agent investigates, adds a note, and submits an allowed resolution.
6. The audit timeline explains every material action.
7. A separate failure scenario demonstrates retries, dead-lettering, recovery, and creation of a new reprocessing job.

## Users and authorization

The public demo provides temporary Manager and Agent identities without registration.

- Managers are intended to inspect organization-wide work, assign cases, run synthetic scenarios, recover demo faults, and reprocess dead jobs.
- Agents are intended to see and update only cases assigned to them.
- I02 persists both memberships and proves the reusable role and tenant guards. Later slices will apply those guards to each domain resource as its API is introduced.
- Demo workspaces expire after four hours and contain only synthetic identities and future synthetic order data.

## System boundaries

```text
React + TypeScript
        |
FastAPI HTTP API and static delivery
        |
SQLAlchemy domain and repositories
        |
PostgreSQL DDL compiled offline / SQLite single-node demo
        |
Database outbox worker (planned)
```

Business authorization remains server-side, database transactions define command boundaries, and the browser receives only the state needed to render the active workspace.

## Reliability model

I02 workspace creation uses a caller-supplied idempotency key scoped to a keyed digest of the client source. The source, key, and canonical payload deterministically derive the opaque session token. Claiming the key, consuming the source limit and capacity, creating the workspace, and completing the receipt share one transaction. An exact retry returns the original logical response and cookie without consuming those limits again; another initial role with the same scoped key returns `409`. The receipt contains no plaintext source address, cookie, or CSRF token.

Role changes use a caller-supplied idempotency key scoped to the source membership and command type. A successful change stores a safe response receipt, revokes the previous session, and links one deterministic replacement session. An exact retry can recover the same result after a lost response; a changed payload with the same key is rejected. Switching to the active role is a no-op that creates neither a session nor a receipt. Real changes are serialized per workspace and limited to 32 by default; exact replays do not consume the quota twice.

Later slices will add these rules:

- Accepted webhook events are immutable.
- Event and initial outbox-job creation share one transaction.
- Worker delivery is at least once; stable database keys make business effects idempotent.
- PostgreSQL workers claim jobs with leases and `FOR UPDATE SKIP LOCKED`.
- The SQLite demo runs one worker with WAL, a busy timeout, and conditional claiming.
- Manual reprocessing preserves the original event, job, and attempts and creates a new linked job.

No feature is described as exactly once or highly available.

## Security and privacy boundaries

I02 enforces opaque cookie sessions, Origin-authority and CSRF checks, bounded request parsing, compact validation errors, server-side tenant and role lookup, persistent creation and role-write limits, trusted-proxy parsing, workspace capacity, and stable secret handling. Uvicorn proxy-header rewriting is disabled, leaving the application as the only `X-Forwarded-For` parser. The detailed assumptions and limits are recorded in the [security model](security-model.md).

The public-history scanner detects recognized credential formats, high-confidence secret assignments, personal email addresses, internal workspace paths, cluster-local hostnames, and RFC1918 addresses. It is a guardrail, not proof that arbitrary customer data is absent; release review still requires synthetic fixtures and human inspection.

## Verification roadmap

Current I02 evidence covers backend API, concurrency, authorization, tenant-isolation, configuration, session-secret, migration, and restart tests; frontend component tests; same-origin hosted smoke tests; desktop and mobile browser journeys; strict type and lint checks; production build output; and public-history scanning.

PostgreSQL SQL and Alembic migration DDL are compiled offline only; no live PostgreSQL migration, transaction, locking, or concurrency test has run. A live PostgreSQL integration job, webhook and worker tests, generated API type consistency, Compose checks, the complete Manager-to-Agent case flow, recovery scenarios, versioned releases, and a released-build walkthrough remain later milestones and are not claimed as current evidence.
