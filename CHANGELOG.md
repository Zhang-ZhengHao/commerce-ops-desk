# Changelog

All notable changes to CommerceOps Desk are documented here. The project follows semantic versioning once a release is published.

## [Unreleased]

### Added

- A compact five-step evaluator guide for creating a synthetic event, testing
  replay and authentication boundaries, assigning the case, working it as an
  Agent, and verifying provenance plus audit history.
- A minimal `GET /api/build` contract for version `0.2.1` and a non-blocking
  footer that renders `v0.2.1` and, for verified images, links the complete
  40-character source SHA to its immutable public commit.
- Persistent entry and internal-note guidance: `Use fictional text only. Do not enter personal, customer, credential, or confidential data.`
- Bounded hourly cleanup for expired synthetic workspaces and obsolete rate-limit windows, with safe failure metadata and a protected capacity sentinel.
- Exact Host allowlisting, hardened-environment documentation shutdown, and versioned workstation Compose/Caddy deployment contracts.
- A root-owned linear Caddy transaction head with current-head-only rollback and explicit fatal-stop reconciliation.
- A visible synthetic portfolio notice with direct source and case-study links.

### Changed

- Verified images now bind one approved source SHA to both the OCI revision
  label and the runtime build identity; candidate checks reject missing,
  malformed, or mismatched identity.
- Runtime deployment evidence now requires an immutable source SHA, 1 CPU, 512 MiB memory, 128 PIDs, and bounded local Docker logs.
- Maintenance scan indexes now include deterministic tie-break columns for SQLite and PostgreSQL.

### Security

- The evaluator contract keeps access-code enforcement at the enterprise
  gateway; CommerceOps adds no access-code field, cookie, API, repository
  setting, or logged secret.
- The enterprise access code must never be stored in or published through this
  repository.
- Free-text notes are not content-scanned. Evaluators remain responsible for
  entering fictional text only and for excluding personal, customer,
  credential, and confidential data.

### Scope

- Live evaluator: single-node SQLite; PostgreSQL 17: CI-verified path only
- This checkpoint does not add or publish a live-demo CTA, and it does not
  claim that the evaluator is deployed, released, or approved for external
  access.
- The exact deployment hostname already exists in versioned engineering files
  and public Git history, so repository URL absence is not a release boundary.
  The actual publication gates are enterprise access-code distribution and
  promotion of the deployment as a live evaluator.
- This checkpoint includes no real Stripe or Shopify integration,
  asynchronous job queue or worker, high availability, production-readiness,
  or production SLA.

## [0.2.0] - 2026-10-08

### Added

- Manager-only synthetic provider controls for fresh delivery, byte-identical replay, one-byte post-signing tamper, and a signature outside the 300-second acceptance window.
- HMAC-authenticated `payment.failed` ingress with a durable inbox, exact replay handling, and one synchronous transaction for the inbox, synthetic order and payment state, case, provenance, and audit effect.
- Safe event provenance in the queue and case detail, limited to the source kind, provider, event type, constrained external event ID, and receipt time.
- Browser recovery that repeats only authoritative GET reads after a committed delivery whose follow-up refresh failed.
- Live PostgreSQL 17 verification for migrations, tenant constraints, transaction and lock races, webhook concurrency, and the hardened production-container readiness path.

### Changed

- The Manager-to-Agent browser journey now continues from signed webhook delivery through assignment, owned-case investigation, an internal note, resolution, and ordered audit history.
- Seeded cases and cases created through the synthetic webhook are visually distinguished; rendered case provenance omits integration IDs, payload digests, signatures, headers, and raw bodies.

### Security

- Webhook ingress enforces the canonical raw path and authenticates the timestamp, integration ID, event ID, and exact raw body before media-type or JSON parsing.
- The demo signer requires the Manager session, same-origin request, and CSRF token, and the route is absent in production.
- The browser keeps the signed envelope only in component memory and delivers it without cookies or other browser credentials.
- Reusing an event ID with a different valid raw body returns `409`; ambiguous delivery outcomes are never retried automatically.

### Scope

- This release is a synthetic, synchronous portfolio slice. It includes no live provider adapter, asynchronous worker or outbox, automatic delivery retry, dead-letter queue, exactly-once guarantee, high-availability claim, performance claim, or production-readiness claim.

## [0.1.0] - 2026-10-07

### Added

- Role-scoped operations dashboard, filtered exception queue, order summary, case detail, internal notes, allowed resolutions, and audit timeline.
- Manager assignment and Agent owned-case workflow with optimistic concurrency and database-backed idempotency.
- Atomic synthetic workspace reset with credential rotation and tenant-isolation coverage.
- Per-workspace note quota, bounded pagination, lifecycle database constraints, and causal audit ordering.
- Desktop, mobile, and 320-pixel browser coverage for the complete Manager-to-Agent path.
- Reproducible multi-stage container packaging with a non-root runtime and persistent demo-data mount.

### Changed

- Case command receipts now store only the affected case ID and resulting version; the frontend reloads the authoritative detail after commit.
- Hosted SQLite defaults to a host-local path instead of the NFS-mounted project directory.

### Security

- Concurrent reuse of one case-command idempotency key with a different payload now returns `409` rather than leaking an integrity failure as `500`.
- Reset requires an authenticated demo tenant, exact same-origin request, and CSRF token.
