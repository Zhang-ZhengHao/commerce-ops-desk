# CommerceOps Desk — Design Summary

## Current verified slice

I03 delivers the first complete order-exception workflow on top of the hosted foundation and demo identity boundary. The I04 slice adds a Manager-only synthetic provider simulator, safe case provenance, and live PostgreSQL evidence to the signed synthetic-event path:

- FastAPI serves the production React bundle and same-origin JSON API from an injected port, with liveness and database-readiness probes.
- Visitors can create a four-hour synthetic Manager or Agent workspace, recover it after refresh, switch persisted roles, and reset only their active tenant.
- The dashboard, filtered queue, detail panel, assignment, notes, allowed resolutions, and accountable timeline operate on migrated database records rather than browser fixtures.
- Managers see organization-wide cases. Agents see and modify only assigned cases. Cross-tenant and unowned record tests verify not-found behavior at the API boundary.
- Assignment, note, and resolution commands use optimistic versions and database-backed idempotency. Receipts contain only a case reference and resulting version, not an expanding copy of the timeline.
- Concurrent exact retries resolve to one effect and the same response. Concurrent reuse of a key for another payload yields one success and one `409`, without partial writes or an unhandled integrity error.
- A per-workspace conditional counter caps synthetic notes. Exact replay does not consume quota twice, and concurrent writes cannot cross the limit.
- Database constraints enforce rule/type/severity mappings, non-null source-event identity, coherent lifecycle fields, valid resolution reasons, and unique per-case audit versions.
- A conditionally registered machine endpoint enforces the canonical raw path, then authenticates the timestamp, integration ID, event ID, and exact body bytes with HMAC-SHA256 before strict media and JSON parsing.
- A separate Manager-only demo route generates the exact signed envelope. It exists only outside production when demo mode and webhook intake are enabled, requires cookie authentication plus Origin and CSRF checks, and accepts only `fresh` or `stale`.
- The simulator can deliver a fresh event, replay the same envelope, tamper with one copied body byte after signing, or request a server-generated timestamp 301 seconds in the past. Public webhook delivery omits browser credentials.
- One `payment.failed` event atomically creates or validates its synthetic order, marks payment failed, creates a new open high-severity payment case, appends a null-actor audit event, and completes its inbox references.
- Exact event/body replays return the committed case without another effect or allowance charge. A different valid raw-body encoding or an immutable order snapshot conflict returns `409`; authentication, source, workspace, and transient-service failures have separate stable codes.
- Webhook-created cases show a synthetic source and only safe provider, event type, external event ID, and receipt-time provenance. Seeded cases remain explicitly identified as demo data.
- The existing live PostgreSQL 17 gate now includes webhook evidence alongside migrations, constraints, readiness, transaction rollback, named lock races, and the production container rather than inferring behavior from compiled SQL.
- Loading, empty, filtered-empty, error, permission, conflict, committed-write/read-failure, reset, desktop, mobile, and 320-pixel states have automated coverage.

The complete signed envelope stays only in panel memory; it is never placed in browser storage, a URL, logs, error objects, or shared application context. The UI renders the allowlisted external event ID, but not the target path, raw body, timestamp, or signature. Replay reuses the original bytes, while tamper and stale scenarios leave the fresh replay cache intact. A successful webhook followed by failed reads is treated as a committed delivery with GET-only recovery, so the UI does not repeat the mutation.

An outbox worker, automated delivery retries, dead-letter recovery, real Stripe or Shopify adapters, overlapping-key rotation, exactly-once delivery, high availability, production-readiness, and performance claims remain unimplemented.

## Product outcome

CommerceOps Desk is designed for a small ecommerce operations team that needs one accountable place to work payment failures, refund reviews, delayed fulfillment, and event-processing failures.

The current executable path is:

1. A temporary workspace receives four deterministic synthetic order exceptions and one isolated synthetic webhook integration.
2. A Manager asks the server for a fresh signed envelope and sends its exact bytes through the public webhook ingress without browser credentials.
3. The HMAC-authenticated inbox and order, case, provenance, and system audit effects commit in one business transaction.
4. The Manager verifies exact replay plus tampered and stale authentication rejection, then assigns the generated case.
5. The active identity switches to Agent with a rotated cookie and CSRF token.
6. The Agent sees only owned work, records an investigation note, and chooses an allowed resolution.
7. The case version, compact command receipt, note, and audit event commit together.
8. The detail view renders safe provenance, notes, and audit history; audit events with equal timestamps remain ordered by case version.

The current integration path accepts one provider-neutral signed synthetic event synchronously and reaches the same case workflow. It is evidence for the boundary and recovery design, not a connection to a live commerce provider.

## Users and authorization

- **Manager:** view all cases in the current organization, list assignable Agents, assign or reassign cases, add notes, resolve, and reset the synthetic workspace.
- **Agent:** view, note, and resolve only cases assigned to the active membership. Agent assignment is forbidden.
- **Tenant boundary:** organization context comes from the authenticated session. Repository queries and conditional writes include that organization; composite foreign keys prevent cross-tenant references.
- **Visibility behavior:** a known but invisible case is returned as `404`, while a role-level capability such as Agent assignment returns `403`.

## System boundaries

```text
React + TypeScript
        | same-origin cookie + CSRF       | raw bytes + HMAC, no cookie
FastAPI API and static delivery
        |                     |                         |
authorization       demo envelope signer      synthetic webhook ingress
        |                     |                         |
        +------------- transactional command/event services --------+
                              |
             SQLAlchemy repositories and Alembic migrations
                              |
       SQLite single-node demo / PostgreSQL 17 verified path
```

The browser holds only the current CSRF token, rendered state, and a panel-local signed envelope. It does not persist session or envelope material in web storage or decide authorization. The signer is cookie-authenticated, but the public webhook delivery explicitly omits credentials. A successful case mutation returns a compact receipt; a subsequent GET obtains the authoritative representation.

## Reliability model

Workspace bootstrap and real role changes use caller-supplied idempotency keys with canonical payload digests. Exact retries recover the stored logical result, while changed payloads return `409`. Role changes rotate and link sessions; selecting the already-active role is a zero-write no-op.

Case commands use `(membership, command type, idempotency key)` as the durable retry scope. The case row is updated with an organization, expected-version, and allowed-status predicate. The corresponding audit event and compact receipt commit in the same transaction. A losing concurrent transaction rolls back completely and can replay only a matching winner; it cannot silently accept a different payload.

Notes use a conditional update of the owning demo organization to enforce the workspace allowance atomically. The 201st unique note returns `429` without advancing the case, counter, receipt, note, or audit history. Replaying an already committed command is checked before quota consumption.

Webhook source admission commits in its own short transaction before target lookup. Authentication resolves a minimal integration projection, then the business transaction re-locks and revalidates organization, provider, enabled state, expiry, and key version. The inbox conflict key binds tenant, integration, and external event ID; the exact raw-body digest distinguishes a replay from conflicting reuse. A database or unknown-commit failure returns retryable `503` without an internal retry.

The browser also avoids guessing about unknown outcomes: it does not automatically retry a network error or `503`. If the delivery response commits but the following dashboard, queue, or detail read fails, a dedicated action reruns only those GET requests and never sends the webhook again.

SQLite uses WAL and a busy timeout only on a host-local filesystem. The hosted launcher deliberately avoids the NFS-backed workspace for its default demo database. PostgreSQL 17 has a separate disposable-database harness so migration, lock, and concurrency claims execute on the real engine.

## Security and privacy boundaries

Implemented controls include opaque cookie sessions, Origin and CSRF checks, a production-absent demo signer, browser delivery without credentials, bounded API bodies, compact validation errors, server-side membership lookup, tenant-scoped data access and provenance, persistent creation and role-write limits, bounded note growth, trusted-proxy parsing, capacity control, stable secret-file handling, raw-byte HMAC verification, dummy authentication work for hidden targets, and keyed source-rate pseudonyms.

All fixtures use fictional identifiers and neutral synthetic order data. The service is disconnected from merchant accounts and cannot issue customer-facing actions. See the [security model](security-model.md) for assumptions and deployment limits.

## Verification evidence

The release gate runs backend API, concurrency, authorization, tenant-isolation, migration, reset, pagination, startup, webhook signer/authentication/transaction, and configuration tests; React component tests; real desktop and mobile browser journeys including fresh, replay, tamper, stale, provenance, assignment, Agent note, and resolution; strict Python and TypeScript checks; a production build; and public-history scanning.

The parallel PostgreSQL gate runs against PostgreSQL 17 and includes the hardened production container. The repository also contains a versioned workstation deployment contract and a scoped blue/green runbook with an explicit privileged-writer boundary; those assets are implementation evidence, not a claim that a particular public deployment has been verified. An outbox/worker, automatic retry scheduler, dead-letter queue, real provider adapter, generated API client, multi-node claims, and released-build performance evidence remain future work and are not represented as complete.
