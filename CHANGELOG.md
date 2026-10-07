# Changelog

All notable changes to CommerceOps Desk are documented here. The project follows semantic versioning once a release is published.

## [Unreleased]

### Added

- Role-scoped operations dashboard, filtered exception queue, order summary, case detail, internal notes, allowed resolutions, and audit timeline.
- Manager assignment and Agent owned-case workflow with optimistic concurrency and database-backed idempotency.
- Atomic synthetic workspace reset with credential rotation and tenant-isolation coverage.
- Per-workspace note quota, bounded pagination, lifecycle database constraints, and causal audit ordering.
- Desktop, mobile, and 320-pixel browser coverage for the complete Manager-to-Agent path.

### Changed

- Case command receipts now store only the affected case ID and resulting version; the frontend reloads the authoritative detail after commit.
- Hosted SQLite defaults to a host-local path instead of the NFS-mounted project directory.

### Security

- Concurrent reuse of one case-command idempotency key with a different payload now returns `409` rather than leaking an integrity failure as `500`.
- Reset requires an authenticated demo tenant, exact same-origin request, and CSRF token.
