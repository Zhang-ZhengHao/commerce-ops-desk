# CommerceOps Desk — Design Summary

## Current verified slice

I03 delivers the first complete order-exception workflow on top of the hosted foundation and demo identity boundary:

- FastAPI serves the production React bundle and same-origin JSON API from an injected port, with liveness and database-readiness probes.
- Visitors can create a four-hour synthetic Manager or Agent workspace, recover it after refresh, switch persisted roles, and reset only their active tenant.
- The dashboard, filtered queue, detail panel, assignment, notes, allowed resolutions, and accountable timeline operate on migrated database records rather than browser fixtures.
- Managers see organization-wide cases. Agents see and modify only assigned cases. Cross-tenant and unowned record tests verify not-found behavior at the API boundary.
- Assignment, note, and resolution commands use optimistic versions and database-backed idempotency. Receipts contain only a case reference and resulting version, not an expanding copy of the timeline.
- Concurrent exact retries resolve to one effect and the same response. Concurrent reuse of a key for another payload yields one success and one `409`, without partial writes or an unhandled integrity error.
- A per-workspace conditional counter caps synthetic notes. Exact replay does not consume quota twice, and concurrent writes cannot cross the limit.
- Database constraints enforce rule/type/severity mappings, non-null source-event identity, coherent lifecycle fields, valid resolution reasons, and unique per-case audit versions.
- Loading, empty, filtered-empty, error, permission, conflict, committed-write/read-failure, reset, desktop, mobile, and 320-pixel states have automated coverage.

The interface exposes only behavior backed by the current API. Signed webhook intake, the outbox worker, retries, dead-letter recovery, and provider adapters remain planned.

## Product outcome

CommerceOps Desk is designed for a small ecommerce operations team that needs one accountable place to work payment failures, refund reviews, delayed fulfillment, and event-processing failures.

The current executable path is:

1. A temporary workspace receives four deterministic synthetic order exceptions.
2. A Manager reviews queue priority and assigns an open case.
3. The active identity switches to Agent with a rotated cookie and CSRF token.
4. The Agent sees only owned work, records an investigation note, and chooses an allowed resolution.
5. The case version, compact command receipt, note, and audit event commit together.
6. The detail view renders notes and audit history; audit events with equal timestamps remain ordered by case version.

The later integration path will begin with a server-signed synthetic webhook and add an immutable event plus transactional outbox before it reaches the same case workflow.

## Users and authorization

- **Manager:** view all cases in the current organization, list assignable Agents, assign or reassign cases, add notes, resolve, and reset the synthetic workspace.
- **Agent:** view, note, and resolve only cases assigned to the active membership. Agent assignment is forbidden.
- **Tenant boundary:** organization context comes from the authenticated session. Repository queries and conditional writes include that organization; composite foreign keys prevent cross-tenant references.
- **Visibility behavior:** a known but invisible case is returned as `404`, while a role-level capability such as Agent assignment returns `403`.

## System boundaries

```text
React + TypeScript
        |
same-origin FastAPI API and static delivery
        |
authorization + transactional command services
        |
SQLAlchemy repositories and Alembic migrations
        |
SQLite single-node demo / PostgreSQL offline DDL target
```

The browser holds only the current CSRF token and rendered state. It does not persist session material in web storage or decide authorization. A successful case mutation returns a compact receipt; a subsequent GET obtains the authoritative representation.

## Reliability model

Workspace bootstrap and real role changes use caller-supplied idempotency keys with canonical payload digests. Exact retries recover the stored logical result, while changed payloads return `409`. Role changes rotate and link sessions; selecting the already-active role is a zero-write no-op.

Case commands use `(membership, command type, idempotency key)` as the durable retry scope. The case row is updated with an organization, expected-version, and allowed-status predicate. The corresponding audit event and compact receipt commit in the same transaction. A losing concurrent transaction rolls back completely and can replay only a matching winner; it cannot silently accept a different payload.

Notes use a conditional update of the owning demo organization to enforce the workspace allowance atomically. The 201st unique note returns `429` without advancing the case, counter, receipt, note, or audit history. Replaying an already committed command is checked before quota consumption.

SQLite uses WAL and a busy timeout only on a host-local filesystem. The hosted launcher deliberately avoids the NFS-backed workspace for its default demo database. PostgreSQL DDL is compiled offline, but live PostgreSQL behavior is not yet claimed.

## Security and privacy boundaries

Implemented controls include opaque cookie sessions, Origin and CSRF checks, bounded API bodies, compact validation errors, server-side membership lookup, tenant-scoped data access, persistent creation and role-write limits, bounded note growth, trusted-proxy parsing, capacity control, and stable secret-file handling.

All fixtures use fictional identifiers and neutral synthetic order data. The service is disconnected from merchant accounts and cannot issue customer-facing actions. See the [security model](security-model.md) for assumptions and deployment limits.

## Verification evidence

The release gate runs backend API, concurrency, authorization, tenant-isolation, migration, reset, pagination, startup, and configuration tests; React component tests; real desktop and mobile browser journeys; strict Python and TypeScript checks; a production build; and public-history scanning.

PostgreSQL SQL and Alembic migration DDL are compiled offline only. A live PostgreSQL service, signed webhook contract, outbox worker, generated API client, Compose deployment, and released-build performance evidence remain future gates and are not represented as complete.
