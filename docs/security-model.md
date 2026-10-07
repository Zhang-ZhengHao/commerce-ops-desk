# CommerceOps Desk Security Model

This document describes controls implemented through I03 and the limits of the public demo. It is an engineering boundary, not a compliance claim or a substitute for an independent production review.

## Threat model

The public endpoint is assumed to receive untrusted browser traffic, malformed JSON, forged forwarding headers, replayed commands, concurrent writes, oversized pagination values, and attempts to cross role or organization boundaries. The demo contains synthetic data and is disconnected from merchant, payment, refund, and fulfillment systems.

Control of the service account, host filesystem, deployment secret store, or database administrator is outside this slice's boundary. TLS termination, edge rate limiting, backups, and trusted-proxy configuration remain deployment responsibilities.

## Session and write controls

- The browser receives a high-entropy opaque cookie. The database stores only a purpose-separated HMAC-SHA256 digest.
- Sessions reference persisted memberships and never duplicate a trusted role claim.
- Cookies are `HttpOnly`, `SameSite=Strict`, and root-scoped. Demo and production default to `Secure`; the ASteam manifest disables it only for the sandbox's direct HTTP endpoint.
- Cookie-authenticated writes require an exact Origin/Host authority match and an in-memory synchronizer token.
- Real role changes revoke the old session and rotate both the cookie and CSRF token. Reset replaces the current organization and credentials atomically.
- The launcher accepts a stable secret value or a regular service-owned `0600` secret file. Production fails closed without an explicit secret and database URL.

## Tenant and role boundaries

Authentication resolves every request through session, membership, user, and organization rows. Case, order, note, audit, membership, and session relationships carry organization identity; composite foreign keys reject cross-tenant references.

Managers may read organization-wide work and assign cases. Agents may read, note, and resolve only cases assigned to their active membership. Role failures return `403`; invisible tenant or record identifiers return `404` so the API does not disclose existence.

Reset is registered only in demo mode, requires an authenticated demo organization plus Origin and CSRF validation, and deletes only that organization. Creation quota or replacement failure rolls back the deletion, leaving the original session usable.

## Transaction and idempotency boundaries

Workspace creation, role switching, assignment, note creation, and resolution have database-backed retry contracts. Canonical payload digests bind a key to one logical command. Exact retries return the stored result; a changed payload returns `409`.

Case writes use organization, version, and allowed-status predicates. The business update, audit event, and compact `{case_id, version}` receipt commit together. Concurrent exact retries create one effect. If two payloads race under the same command key, the loser rolls back and returns a conflict only after finding the committed winner; unrelated integrity failures are not swallowed.

Database checks reinforce service rules:

- rule key, case type, and severity must be one approved combination;
- every case has a source-event identifier;
- open, assigned, and resolved lifecycle fields must be coherent;
- resolution reasons must belong to the case rule;
- each material case version has at most one audit event.

The application creates audit rows only by append. This prevents accidental API updates or duplicates but does not claim tamper resistance against a database administrator.

## Abuse and resource controls

- API writes have a configured byte ceiling enforced before request parsing. Validation failures do not echo attacker-controlled input.
- Workspace creation uses a persistent fixed-hour counter keyed by a purpose-separated digest of the client source; raw addresses are not stored there.
- The hosted process disables Uvicorn proxy rewriting. `X-Forwarded-For` is considered only for a direct peer in the configured trusted-proxy set.
- A stable database lock row serializes the active-workspace capacity check.
- Real role changes have a persistent per-workspace allowance. Selecting the current role is a zero-write operation.
- Demo notes have a persistent per-workspace allowance of 200 by default. A conditional update prevents concurrent writes from crossing the boundary; exact replay is free.
- List endpoints bound both page size and page number, so an oversized SQLite offset cannot become an unhandled server error.

These controls reduce accidental and low-cost abuse. They do not replace upstream connection, bandwidth, or distributed denial-of-service protection.

## Data and deployment limits

All included identities and order records are fictional. No customer email, address, payment instrument, merchant credential, or live provider token is required or stored.

The public demo is single-node. SQLite WAL is used only on a host-local path; tests explicitly avoid the NFS-mounted workspace after reproducing lock stalls there. The default hosted database is disposable across container replacement unless the operator provides an explicit durable database URL.

PostgreSQL models and Alembic DDL compile offline, but no live PostgreSQL migration, transaction, lock, or concurrency result is claimed. Multi-node operation, centralized rate limiting, key rotation, encrypted backups, observability, disaster recovery, and formal compliance remain outside this revision.
