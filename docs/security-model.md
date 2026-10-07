# CommerceOps Desk Security Model

This document describes the controls implemented in I02 and the limits of the public demo. It is an engineering boundary, not a compliance claim or a substitute for an independent production review.

## Threat model

The public endpoint is assumed to receive untrusted browser traffic, malformed JSON, forged headers, repeated workspace requests, replayed commands, and attempts to cross organization or role boundaries. The demo contains synthetic data and is intentionally disconnected from merchant, payment, refund, and fulfilment systems.

An attacker with control of the service account, host filesystem, deployment secret store, or database is outside this slice's security boundary. TLS termination and trusted-proxy configuration are deployment responsibilities.

## Session and CSRF controls

- A browser receives a high-entropy opaque cookie. The database stores only a purpose-separated HMAC-SHA256 digest of that token.
- Session records reference a persisted membership; they do not duplicate or trust a role claim.
- Cookies are `HttpOnly`, `SameSite=Strict`, and root-scoped. Demo and production configurations default to `Secure`; the ASteam manifest disables that flag only because its sandbox product endpoint is exposed directly over HTTP.
- Cookie-authenticated writes require the `Origin` host and port to match `Host`, plus a synchronizer token held only in application memory. The application does not claim to reconstruct an external scheme behind a TLS-terminating proxy.
- A role change revokes the old session and rotates both the cookie and CSRF token. A revoked cookie cannot resume an ordinary authenticated request.
- The single-node launcher generates one stable private secret for non-production demo use. A custom file must be a regular file owned by the service user with mode `0600`. Production fails closed unless a secret value or file is explicitly configured.

## Tenant and role boundaries

Organization IDs are persisted across users, memberships, sessions, and command receipts. Composite foreign keys prevent a row from silently linking identities across organizations. Authentication rebuilds its context by joining those authoritative rows on every request.

Demo role routes additionally require a demo organization. Role guards return `403` when an authenticated identity lacks permission. Tenant-visibility guards return `404` for a resource in another organization so the API does not confirm that the record exists.

I02 proves these boundaries with identity and guard tests. Domain records such as exception cases are not implemented yet, so no claim is made about their future authorization until those slices add their own API tests.

## Abuse controls

- API writes have a configured byte ceiling enforced before Pydantic parses the body. Oversized requests receive a small `413` response.
- Validation failures return a compact error without echoing attacker-controlled input.
- Workspace creation uses a persistent fixed-hour counter keyed by a purpose-separated digest of the client source; raw source addresses are not stored in that table.
- The hosted launcher passes `--no-proxy-headers`, so Uvicorn does not rewrite the ASGI peer. The application is the sole `X-Forwarded-For` parser: it considers the header only when the direct peer is in the configured trusted-proxy set and walks the chain from right to left.
- A stable database lock row serializes the active-workspace capacity check. SQLite concurrency tests exercise this boundary; PostgreSQL behavior is compiled offline only.
- Real role changes are serialized per workspace and capped by a persistent configurable quota. Switching to the already-active role creates no session or receipt, and exact command replay does not consume the quota twice.

These controls reduce accidental and low-cost abuse. They do not replace upstream connection, bandwidth, or distributed denial-of-service protection.

## Database-backed idempotency

Each workspace-creation command requires an idempotency key scoped to a purpose-separated digest of the client source. The canonical payload digest and safe result references are committed in the same transaction as capacity and source-limit consumption. The response cookie is deterministically derived from the deployment secret and command identity, allowing serial, concurrent, and post-restart retries to recover the same workspace without another capacity or rate-limit charge. Reusing the scoped key for another initial role returns a conflict. The receipt stores neither the raw source address nor plaintext session or CSRF material.

Each role-change command requires an idempotency key scoped to the source membership and command type. The canonical payload digest, safe response body, and resulting membership and session IDs are committed together.

The replacement token is derived deterministically from the authenticated source token and command identity. An exact retry, including one made after the browser has accepted the replacement cookie, resolves the stored replacement and returns the same logical response. Reusing the key for a different role returns a conflict. Plain session or CSRF tokens are never stored in the receipt.

## Deployment limits

The public demo is a single-node SQLite deployment using WAL and a busy timeout. Its persistent secret file and SQLite database must live on durable storage if sessions are expected to survive container replacement. Expired workspaces are denied at authentication time; scheduled deletion is planned for a later operational slice.

PostgreSQL SQL and Alembic migration DDL are compiled offline only; no live PostgreSQL migration, transaction, locking, or concurrency test has run. Multi-node operation, key rotation, centralized rate limiting, proxy-specific deployment validation, backups, observability, and high availability are not claimed by this revision.
