# CommerceOps Desk Security Model

This document describes controls implemented through the I04 signed-ingress backend slice and the limits of the public demo. It is an engineering boundary, not a compliance claim or a substitute for an independent production review.

## Threat model

The public endpoint is assumed to receive untrusted browser and machine traffic, malformed JSON, forged forwarding headers, replayed commands and events, concurrent writes, oversized requests and pagination values, invalid signatures, and attempts to cross role or organization boundaries. The demo contains synthetic data and is disconnected from merchant, payment, refund, and fulfillment systems.

Control of the service account, host filesystem, deployment secret store, or database administrator is outside this slice's boundary. TLS termination, edge rate limiting, backups, and trusted-proxy configuration remain deployment responsibilities.

## Session and write controls

- The browser receives a high-entropy opaque cookie. The database stores only a purpose-separated HMAC-SHA256 digest.
- Sessions reference persisted memberships and never duplicate a trusted role claim.
- Cookies are `HttpOnly`, `SameSite=Strict`, and root-scoped. Demo and production default to `Secure`; the ASteam manifest disables it only for the sandbox's direct HTTP endpoint.
- Cookie-authenticated writes require an exact Origin/Host authority match and an in-memory synchronizer token.
- Real role changes revoke the old session and rotate both the cookie and CSRF token. Reset replaces the current organization and credentials atomically.
- The launcher accepts a stable secret value or a regular service-owned `0600` secret file. Production fails closed without an explicit secret and database URL.

## Signed webhook boundary

- Webhook intake is registered only when enabled with a dedicated master secret of at least 32 UTF-8 bytes. Local settings and production default it off; the hosted demo launcher enables it and creates or reuses an isolated mode-`0600` secret file. It never reuses the browser session secret.
- The route identifier must be a canonical lowercase UUID in the exact raw ASCII path. Security headers must each occur exactly once and use closed timestamp, event-ID, and signature grammars.
- A per-integration key is derived from the master secret, canonical integration ID, and positive key version. HMAC-SHA256 covers the version marker, timestamp, integration ID, event ID, and exact raw body bytes. Verification happens before media-type or JSON inspection, and comparison is constant-time.
- Timestamps exactly 300 seconds in the past or future are accepted; values beyond that window fail. Retrying a committed event uses the same event ID and body with a current timestamp and signature.
- Organization identity comes only from the authenticated integration join. The payload cannot select a tenant. Unknown, disabled, non-demo, expired, malformed, and version-rotated targets perform HMAC work and share the same compact `401` response.
- The closed payload accepts one synthetic `payment.failed` event shape. It rejects extra customer fields, duplicate JSON keys, non-standard numeric constants, invalid UTF-8, a BOM, trailing data, compression, and unsupported content types.
- The service stores a SHA-256 digest of the exact body, constrained event metadata, and safe result references. It never stores the body, signature, derived key, master secret, request headers, source address, session token, or arbitrary customer text.
- Inbox claim, demo allowance, order snapshot, payment state, case, system audit, and result references commit once in one business transaction. Exact body replays return the stored case without consuming allowance; reuse of an event ID with a different valid raw-body encoding or an immutable order mismatch returns `409`.
- A `503` may mean the commit result was not observed. The service does not retry internally; the sender safely retries the same event ID and body with fresh authentication material. The receiver safely supports retries from an at-least-once sender; it does not claim exactly-once delivery.

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
- Webhook attempts first consume a persistent per-source minute bucket in an independent short transaction. The stored key is a domain-separated HMAC digest, so later authentication, validation, conflict, or business rollback does not refund the attempt or persist the raw address.
- The hosted process disables Uvicorn proxy rewriting. `X-Forwarded-For` is considered only for a direct peer in the configured trusted-proxy set.
- The hosted process also disables Uvicorn access logging. Expected webhook failures do not emit exception logs; unexpected failures log only a server-generated request ID, stable phase and code, and exception class name.
- A stable database lock row serializes the active-workspace capacity check.
- Real role changes have a persistent per-workspace allowance. Selecting the current role is a zero-write operation.
- Demo notes have a persistent per-workspace allowance of 200 by default. A conditional update prevents concurrent writes from crossing the boundary; exact replay is free.
- List endpoints bound both page size and page number, so an oversized SQLite offset cannot become an unhandled server error.

These controls reduce accidental and low-cost abuse. They do not replace upstream connection, bandwidth, or distributed denial-of-service protection.

## Data and deployment limits

All included identities and order records are fictional. No customer email, address, payment instrument, merchant credential, or live provider token is required or stored.

The public demo is single-node. SQLite WAL is used only on a host-local path; tests explicitly avoid the NFS-mounted workspace after reproducing lock stalls there. The default hosted database is disposable across container replacement unless the operator provides an explicit durable database URL.

PostgreSQL 17 migrations, tenant constraints, readiness, selected lock/concurrency behavior, webhook transactions, and the production container path are exercised against a live disposable database in CI. This does not establish multi-node throughput or availability. Edge bandwidth protection, distributed rate limiting, overlapping-key rotation, asynchronous event delivery, encrypted backups, production observability, disaster recovery, and formal compliance remain outside this revision.
