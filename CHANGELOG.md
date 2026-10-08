# Changelog

All notable changes to CommerceOps Desk are documented here. The project follows semantic versioning once a release is published.

## [Unreleased]

## [0.2.0] - 2026-10-08

### Added

- Manager-only synthetic provider controls for fresh delivery, byte-identical replay, one-byte post-signing tamper, and a signature outside the 300-second acceptance window.
- HMAC-authenticated `payment.failed` ingress with a durable inbox, exact replay handling, and one synchronous transaction for the inbox, synthetic order and payment state, case, provenance, and audit effect.
- Safe event provenance in the queue and case detail, limited to the provider, event type, constrained external event ID, and receipt time.
- Browser recovery that repeats only authoritative GET reads after a committed delivery whose follow-up refresh had an unknown outcome.
- Live PostgreSQL 17 verification for migrations, tenant constraints, transaction and lock races, webhook concurrency, and the hardened production-container readiness path.

### Changed

- The Manager-to-Agent browser journey now continues from signed webhook delivery through assignment, owned-case investigation, an internal note, resolution, and ordered audit history.
- Seeded cases and cases created through the synthetic webhook are visually distinguished without exposing integration IDs or signing material.

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
