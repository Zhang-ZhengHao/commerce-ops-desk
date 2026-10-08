# I04 design: signed synthetic webhook with live PostgreSQL proof

Status: approved direction for implementation planning

Target release: CommerceOps Desk v0.2.0

Data boundary: synthetic demo records only

## 1. Decision

I04 will prove one narrow external-event path end to end:

1. CI runs the existing application and migrations against a real PostgreSQL service.
2. A provider-neutral synthetic sender delivers one signed `payment.failed` event.
3. The application authenticates the raw request, admits the event idempotently, and creates the order snapshot, exception case, and audit record in one database transaction.
4. The Manager sees the new case in the existing queue and can complete the existing Manager-to-Agent workflow.

The work is one release direction split across three implementation pull requests:

1. live PostgreSQL migration, constraint, transaction, container-readiness, and concurrency proof;
2. signed webhook inbox and synchronous `payment.failed` processing;
3. Manager-only synthetic-provider simulator and visible event provenance in the case workspace.

This split creates reviewable public history without pretending that an asynchronous integration platform already exists.

## 2. Why this scope

The current release already demonstrates tenant-scoped application commands, retry-safe writes, case assignment, notes, resolution, and audit history. Its largest evidence gaps are explicit in the repository:

- PostgreSQL behavior is compiled offline but never exercised against a server.
- No external event crosses an authenticated machine-to-machine boundary.
- The user interface begins with pre-seeded cases, so a prospective client cannot watch a new external event become accountable work.

PostgreSQL-only work would improve engineering evidence but would be almost invisible in a product demonstration. Webhook-only work would be visible but would leave the strongest database claims unproved. Combining them in one release, while keeping each pull request small, produces both forms of evidence.

## 3. Goals and non-goals

### Goals

- Prove fresh and repeat PostgreSQL migrations, database readiness, tenant constraints, and selected concurrency semantics in public CI.
- Authenticate webhook bytes with HMAC-SHA256 before JSON parsing.
- Resolve the target organization only from a server-side integration record.
- Accept at-least-once delivery while producing one committed business effect for exact replays.
- Reject reuse of one provider event ID with different bytes.
- Commit the inbox record, order snapshot, exception case, and system audit atomically.
- Preserve the existing case workflow and authorization boundaries.
- Let a Manager demonstrate success, exact replay, tampering, and stale-signature behavior without seeing a signing secret.
- Retain only bounded, non-sensitive event metadata needed for provenance and replay decisions.

### Non-goals

- Stripe, Shopify, or any other real provider adapter.
- Outbox tables, background workers, retries, dead-letter queues, or event redrive.
- Exactly-once delivery claims.
- Refund, fulfillment, or event-ordering semantics.
- Raw webhook payload retention.
- A production secret-management UI, integration-provisioning API, or overlapping two-key rotation window.
- Multi-node migration orchestration, high-availability claims, load-test claims, or compliance claims.
- Real customer, order, payment, or personal data.

## 4. Components and boundaries

### 4.1 PostgreSQL verification harness

A dedicated test harness creates a uniquely named temporary database from the PostgreSQL administrative database, applies migrations, runs the selected integration suite, disposes every SQLAlchemy engine, terminates remaining test connections, and drops the database. Existing SQLite tests remain unchanged and continue to protect the hosted single-node demo path.

The harness is separate from the existing fixtures because those fixtures deliberately replace the configured URL with SQLite. Merely exporting a PostgreSQL URL and rerunning them would create a false green result.

### 4.2 Webhook integration

Each demo organization receives one `synthetic` integration when its workspace is created. A synthetic integration may belong only to an organization with `is_demo = true`; creation services reject every other organization. The integration's canonical lowercase, hyphenated UUID is the public route identifier. It is random, but it is not treated as an authentication credential; only a valid HMAC authenticates the request.

The route is:

```text
POST /api/webhooks/synthetic/{integration_id}
```

The route does not use a browser cookie, CSRF token, or user membership. Its path parameter remains a raw string so the handler—not framework UUID coercion—can give malformed values the same generic response. The handler compares the ASGI raw path against the exact ASCII prefix plus canonical UUID, rejecting percent-encoded or alternative UUID spellings. It joins the integration to its organization and accepts it only when the provider is `synthetic`, the integration is enabled, the organization is a demo, and the workspace has not expired. It never accepts an organization identifier from the payload. Subject to the pre-authentication source limit, malformed, unknown, disabled, non-demo, and expired targets share one generic authentication-failure response.

### 4.3 Webhook inbox

The inbox is independent of browser `CommandReceipt` records. Browser receipts are tied to a membership and session; webhook identity, replay, and retention have different boundaries.

One inbox row records the provider event identifier, event type, digest of the exact body bytes, safe result references, and receipt times. It never stores the body, signature, signing key, session token, request headers, IP address, or customer PII.

### 4.4 Synchronous event processor

The processor handles only `payment.failed`. It uses the existing `payment_failed` case rule, creates or validates a tenant-scoped order snapshot, creates an open high-severity case, and appends a system audit event with a `NULL` actor. All writes occur in the request transaction.

The synchronous boundary is deliberate. An outbox and worker become valuable only when the project adds a real downstream action or independent availability boundary. Adding them now would create machinery without a truthful product requirement.

### 4.5 Synthetic-provider simulator

A Manager-only demo endpoint creates a signed envelope for the Manager's own active integration. It is registered only in demo mode, requires the existing same-origin and CSRF checks, and accepts only a closed `fresh` or `stale` scenario enum. The server—not the client—chooses the integration ID, event ID, timestamp, order identifiers, amount, currency, and exact body. It returns the endpoint path, exact JSON body string, timestamp, event ID, and signature to the browser, but never returns the derived signing key or master secret. The response uses `Cache-Control: no-store`; the signature and body are excluded from application, access, telemetry, and error logs. The browser then performs a normal `fetch` to the public webhook route.

The simulator therefore exercises the same request-body limit, timestamp check, HMAC verification, JSON validation, inbox claim, and transaction as an external sender. It does not call the event service directly.

## 5. Data model

Migration `0005_webhook_inbox` adds the following records and constraints.

### 5.1 `webhook_integrations`

| Column | Rule |
|---|---|
| `id` | UUID string primary key and opaque public route identifier |
| `organization_id` | required tenant foreign key with cascade delete |
| `provider` | required; check-constrained to `synthetic` in I04 |
| `key_version` | required integer, at least 1 |
| `enabled` | required boolean |
| `created_at` | required timezone-aware timestamp |
| `updated_at` | required timezone-aware timestamp |

Constraints:

- unique `(organization_id, id)` for tenant-safe composite references;
- unique `(organization_id, provider)` so one workspace cannot receive two synthetic integrations;
- index on `(id, enabled)` for ingress lookup.

### 5.2 `webhook_events`

| Column | Rule |
|---|---|
| `id` | UUID string primary key; also used as `ExceptionCase.source_event_id` |
| `organization_id` | required tenant foreign key with cascade delete |
| `integration_id` | required composite tenant foreign key |
| `external_event_id` | required synthetic identifier matching `^evt_[A-Za-z0-9]{8,64}$` |
| `event_type` | required; `payment.failed` in I04 |
| `payload_digest` | lowercase SHA-256 hex of the exact raw body |
| `occurred_at` | required timezone-aware provider event time |
| `order_id` | nullable only while the uncommitted claim is being processed |
| `case_id` | nullable only while the uncommitted claim is being processed |
| `received_at` | required server receipt time |
| `processed_at` | set before the same transaction commits |

Constraints:

- unique `(organization_id, integration_id, external_event_id)`;
- tenant-safe composite foreign keys for integration, order, and case;
- check constraints for the digest shape and for result fields being either all empty during processing or all populated when processed;
- index on `(organization_id, received_at)` for provenance queries.

The service invariant is that no transaction commits an inbox row without `order_id`, `case_id`, and `processed_at`. Failure-injection tests enforce this even where an intermediate nullable state is needed for an atomic claim.

### 5.3 Demo workspace allowance

`organizations` receives `webhook_event_count`, defaulting to zero. A new unique event conditionally increments this counter in the same transaction and is rejected after `demo_webhook_event_limit`, default 20. Exact replays do not consume the allowance. Synthetic integrations and events cannot target non-demo organizations.

Deleting or resetting a demo workspace cascades to its integration and inbox records.

## 6. Signing and key contract

### 6.1 Required headers

```text
Content-Type: application/json
X-Webhook-Timestamp: <canonical Unix seconds>
X-Webhook-Event-Id: <synthetic event ID>
X-Webhook-Signature: v1=<64 lowercase hexadecimal characters>
```

The route accepts only its canonical lowercase, hyphenated UUID spelling. The timestamp must match `^[1-9][0-9]{0,9}$`: unsigned ASCII decimal, no whitespace, sign, or leading zero, with at most ten digits. The event ID must match `^evt_[A-Za-z0-9]{8,64}$`. Duplicate security headers, folded values, unsupported signature versions, and non-canonical hexadecimal signatures are rejected.

### 6.2 Signed bytes

The exact byte sequence is:

```text
v1\n{timestamp}\n{integration_id}\n{event_id}\n{raw_body}
```

The first four components are strict ASCII. `{integration_id}` is the canonical database value and the route is rejected unless its raw path value has that exact spelling. `raw_body` is appended exactly as received. Whitespace changes therefore invalidate the signature. The expected signature is:

```text
hex(HMAC-SHA256(derived_integration_key, signed_bytes))
```

Comparison uses `hmac.compare_digest`.

### 6.3 Secret configuration and rotation

Webhook signing uses a dedicated `COMMERCE_OPS_WEBHOOK_MASTER_SECRET`; it never reuses the browser session secret. A direct environment value is encoded as UTF-8 exactly and must contain at least 32 bytes. `COMMERCE_OPS_WEBHOOK_MASTER_SECRET_FILE` supports container secret mounts: the launcher reads bytes, removes exactly one terminal LF and an immediately preceding CR when present, rejects NUL bytes or any other leading/trailing ASCII whitespace, validates the remaining length, and exports the UTF-8 value. When both sources are present, the direct environment value wins and the file is not read.

The per-integration key is derived server-side:

```text
HMAC-SHA256(
  webhook_master_secret,
  "commerce-ops:synthetic-webhook-key:v1\n" + integration_id + "\n" + key_version
)
```

The integration ID and positive key version are encoded as canonical ASCII; the key version is unsigned decimal without leading zero. The 32-byte HMAC digest is used directly as the event-signing key. The database stores only `key_version`, never the derived key or master secret. Increasing `key_version` immediately rotates that integration and invalidates old signatures. Resetting a demo workspace also produces a new integration ID and therefore a new key. An overlapping old/new acceptance period and external secret distribution are deliberately deferred until a real provider adapter exists.

In demo mode, the hosted launcher may create a stable mode-0600 secret file beside the existing session-secret file. In production, enabling webhook intake requires an explicit environment or secret-file source; startup fails closed when it is missing or cannot be decoded as UTF-8. Secrets are never committed, logged, returned by APIs, or embedded in frontend code.

### 6.4 Pre-authentication abuse control

Before integration lookup, ingress applies a durable per-source minute bucket with `webhook_source_minute_limit`, default 120. Source addresses use the existing trusted-proxy normalization rules. The database stores only `HMAC-SHA256(webhook_master_secret, "commerce-ops:webhook-source:v1\n" + normalized_source)`, never the address. The limiter uses the existing dialect-specific atomic upsert pattern and returns `429 webhook_ingress_rate_limited` with `Retry-After` before random integration IDs can trigger unbounded target lookups. A deployment may add an edge limit, but the public demo does not depend on an undocumented gateway control.

## 7. Payload contract

I04 accepts exactly this shape, with all extra fields forbidden:

```json
{
  "type": "payment.failed",
  "occurred_at": "2026-10-07T12:34:56Z",
  "data": {
    "order": {
      "id": "syn_order_A1B2C3D4",
      "number": "DEMO-1045",
      "amount_minor": 12900,
      "currency": "USD"
    }
  }
}
```

Validation rules:

- `type` is exactly `payment.failed`;
- `occurred_at` must include a timezone and is normalized to UTC;
- order ID matches `^syn_order_[A-Za-z0-9]{8,48}$`;
- order number matches `^DEMO-[0-9]{4,10}$`;
- `amount_minor` is an integer from 0 through 999,999,999;
- currency is exactly three uppercase ASCII letters;
- no organization, integration, status, assignee, resolution, customer, email, address, card, or free-text fields are accepted.

The provider event ID exists only in its signed header, avoiding two competing identifiers in the request. The deliberately synthetic identifier grammars, demo-only integration binding, and closed schema prevent this endpoint from accepting names, emails, addresses, card data, arbitrary customer text, or live-provider identifiers.

The media contract is also closed: `Content-Encoding` must be absent, and `Content-Type` must be `application/json` with either no parameter or the single case-insensitive parameter `charset=utf-8`. After authentication, the body must decode as UTF-8 without a BOM. The JSON parser rejects duplicate object keys, `NaN`, `Infinity`, `-Infinity`, trailing data, non-object top levels, and every other non-standard JSON form before Pydantic validation.

## 8. Request and transaction flow

1. The existing middleware rejects a body larger than 16 KiB before route parsing.
2. The handler reads the raw body once and applies the pre-authentication source limit.
3. It strictly parses the canonical route UUID and required security headers without decoding JSON.
4. It joins the integration to its organization and evaluates provider, enabled, demo, and expiry state. For a malformed, unknown, disabled, non-demo, or expired target, it still performs one HMAC calculation with a derived dummy key before following the same external failure path as a bad signature; the response never reveals which check failed.
5. It derives the integration key and verifies the HMAC over the exact raw body before inspecting media metadata or decoding JSON.
6. It rejects timestamps more than 300 seconds old or more than 300 seconds in the future, using the injected server clock. Exactly minus or plus 300 seconds is accepted.
7. It validates `Content-Type` and the absence of `Content-Encoding`, decodes strict UTF-8 without BOM, parses strict JSON without duplicate keys or non-standard constants, and validates the closed payload schema.
8. It computes the SHA-256 body digest and atomically claims `(organization, integration, external_event_id)` with the database's native conflict-safe insert.
9. If a committed row already exists with the same digest, it returns the stored case result as a replay even when the authenticated retry has a newer timestamp and signature. If the digest differs, it returns a conflict.
10. For a new event, it conditionally consumes one workspace event allowance.
11. It uses a dialect-native conflict-safe insert for the tenant-scoped order, then reads the winner, verifies that its number, amount, and currency match, and sets its payment status to `failed`. This also handles two different events concurrently introducing the same order without turning the expected uniqueness race into a `500`.
12. It creates an open `payment_failed` case with version 1 and the existing two-hour SLA. `source_event_id` is the internal inbox UUID, not the provider-controlled ID.
13. It appends `case.created_from_webhook` with a `NULL` actor. Audit changes include only provider name, event type, constrained external event ID, and resulting version.
14. It fills the inbox result references and commits once.

If any step after the claim fails, the whole transaction rolls back. The sender retries with the same event ID and byte-identical body but generates a current timestamp and matching signature when the previous timestamp is stale. Under PostgreSQL `READ COMMITTED`, a competing insert waits for the winning transaction; after commit it replays the winner, and after rollback it can become the new winner.

The implementation must not use a check-then-insert sequence and must not swallow an arbitrary `IntegrityError`. When a uniqueness race aborts a PostgreSQL transaction, the session is rolled back before reading the committed winner.

## 9. Response and retry semantics

All error bodies use `{"detail":{"code":"...","message":"..."}}` with stable codes and safe messages; none echo the body, signature, secret, tenant, or integration state.

| Condition | Status | Sender action |
|---|---:|---|
| New event fully committed | `201` | stop |
| Exact replay, same ID and bytes | `200` | stop |
| Malformed/unknown/disabled/non-demo/expired target, invalid authentication header, bad signature, stale/future timestamp | `401 webhook_authentication_failed` | fix authentication; do not blind-retry |
| Unsupported media type | `415` | fix request |
| Valid signature but malformed/invalid JSON or unsupported event type | `422` | fix payload |
| Same event ID with different body digest or conflicting immutable order snapshot | `409` | investigate; use a new legitimate event ID only for a new event |
| Pre-authentication source limit reached | `429 webhook_ingress_rate_limited` | wait for `Retry-After` |
| Demo event allowance exhausted | `429 demo_webhook_limit_reached` | reset the demo workspace |
| Body exceeds limit | `413` | reduce request |
| Transient database failure or unknown commit outcome | `503` with `Retry-After` | retry the same event ID and body with a current timestamp/signature |

Success bodies contain only:

```json
{
  "status": "processed",
  "event_id": "evt_A1B2C3D4",
  "case_id": "internal-case-uuid",
  "replayed": false
}
```

An exact replay returns the same event and case identifiers with `replayed: true`. Idempotency is bound to integration, event ID, and body bytes; timestamp and signature are authentication material and may be refreshed for a retry.

## 10. User experience

Only the Manager view displays a compact **Synthetic provider** panel. It explains that no real store or payment processor is connected and offers four deterministic demonstrations:

1. **Deliver new failure** — requests a signed envelope, sends it to the real webhook endpoint, refreshes the dashboard and queue, and opens the resulting case.
2. **Replay same event** — resends the exact cached envelope while it is fresh and shows that the same case was returned with no duplicate business effect.
3. **Tamper after signing** — changes one body byte and shows the generic authentication rejection.
4. **Send stale signature** — requests an envelope outside the five-minute window and shows the authentication rejection.

The UI shows the provider event ID, event type, received time, and replay outcome. It never renders a signing key, master secret, session token, full request headers, or arbitrary raw payload.

Webhook-created cases display a `Synthetic webhook` source badge and a provenance block in case detail. Seeded cases display `Seeded demo data`; the existing queue, assignment, note, resolution, and audit behavior remains compatible.

Loading, success, exact-replay, authentication rejection, validation rejection, limit, network failure, and “committed but refresh failed” states receive explicit accessible copy. Buttons are disabled during their own request, keyboard reachable, and usable at the existing 320 px viewport boundary.

## 11. PostgreSQL CI evidence

The existing SQLite verification job remains. A parallel `postgres-integration` job uses an immutable digest for a PostgreSQL 17 Alpine service and `pg_isready` health checks. It installs locked Python dependencies only; frontend and browser dependencies remain in the existing job.

The job must prove:

- fresh `alembic upgrade head` on an empty database;
- a second upgrade is a no-op;
- `alembic check` reports no model drift;
- `head -> 0004_order_case -> head` migration round trip;
- an organization created at `0004_order_case` receives exactly one synthetic integration on upgrade, and a repeated upgrade does not duplicate it;
- `/ready` succeeds against the live service;
- demo bootstrap and one Manager-to-Agent case workflow execute on PostgreSQL;
- tenant composite foreign keys and check constraints reject invalid records;
- PostgreSQL branches for rate limiting, capacity locking, and bootstrap idempotency execute;
- exact concurrent browser-command retries create one effect;
- conflicting payloads for one browser key produce one success and one stable conflict;
- competing updates from one case version produce one winner;
- concurrent reset has one winner;
- exact concurrent webhook deliveries create one inbox row, order, case, and audit event;
- different concurrent events for one previously unseen order share one validated order and create their own event-scoped cases without a uniqueness error;
- same event ID with different bytes never produces a `500` or partial commit;
- two tenants may use the same provider event ID;
- an injected failure between inbox claim and case completion leaves no row or counter change committed.

Concurrency tests synchronize with barriers or explicit hooks, never sleeps.

The job also builds the production container, connects it to the PostgreSQL service, waits for `/ready`, and verifies the application-reported database dependency. `/health` alone is not accepted as database evidence.

## 12. Test strategy

### Unit and contract tests

- exact signing bytes and key derivation vectors;
- constant-time comparison path;
- canonical raw-path UUID/timestamp/event-ID grammar, percent-encoding rejection, and duplicate-header rejection;
- current, lower-bound, upper-bound, stale, and future timestamps with an injected clock;
- closed payload schema, synthetic identifier patterns, and every length/range boundary;
- strict UTF-8/JSON behavior for BOM, duplicate keys, non-standard constants, trailing data, media type, charset, and content encoding;
- settings validation, exact secret-file newline handling, direct-value precedence, fail-closed startup, and log redaction;
- keyed source-rate digest and minute-bucket admission without persisted raw addresses;
- migration model parity and downgrade/upgrade behavior;
- UI state reducer and API-client response handling.

### SQLite integration tests

- valid delivery and visible case provenance;
- invalid signature plus invalid JSON returns authentication failure, proving verification precedes parsing;
- byte-level body mutation fails;
- malformed, unknown, disabled, non-demo, and expired integrations share the authentication response and cannot write;
- exact replay returns one effect;
- same event ID and body with a refreshed valid timestamp/signature returns the committed effect;
- changed-payload reuse conflicts;
- cross-tenant route and body forgery attempts cannot change the target tenant;
- atomic rollback after injected failures;
- source-rate limiting occurs before integration lookup and honors only trusted proxy hops;
- demo event allowance and reset cascade;
- `0004_order_case -> head` with existing demo data creates one usable integration on both SQLite and PostgreSQL;
- Manager-only simulator authorization, CSRF enforcement, closed scenario input, server-owned envelope fields, `no-store`, and log redaction;
- Agent visibility remains limited to assigned cases.

### PostgreSQL integration tests

The public CI evidence listed in section 11 is a release gate. Tests assert counts and returned identifiers, not only status codes.

### Browser tests

- successful new delivery appears in dashboard, queue, detail, and audit history;
- exact replay keeps the same case and visible counts;
- tampered and stale scenarios visibly fail without changing counts;
- the new case can be assigned, opened as Agent, noted, and resolved;
- desktop, mobile, and 320 px layouts do not overflow or hide controls;
- keyboard focus and live status messages remain usable.

## 13. Deployment and migration behavior

- The application version becomes `0.2.0` only when all three pull requests are merged and the release gate is green.
- `0005_webhook_inbox` is backward-compatible with current data. It creates one synthetic integration for each existing demo organization so an in-flight four-hour workspace does not lose the new panel, but it creates no inbox history for existing seeded cases. Normal expiry cleanup removes integrations belonging to stale workspaces.
- The runtime applies the migration before serving, as it does today. This remains a documented single-instance/demo behavior; multi-replica migration coordination is still outside scope.
- Webhook routes are registered only when the feature is enabled and a valid master-secret source exists.
- Hosted demo startup generates or reads a stable local webhook secret with restrictive permissions. Production never generates one implicitly.
- Rollback disables ingress first, then rolls the application back, then downgrades only if no retained I04 data is needed. The migration downgrade removes webhook records and the demo counter, so it is intentionally data-destructive and is not an automatic deploy step.

## 14. Public evidence and truthful claims

Each implementation pull request must include its exact commands and green CI link. The v0.2.0 release will include:

- a real browser recording of the successful delivery, exact replay, tamper rejection, and Manager-to-Agent resolution path;
- the public PostgreSQL job and named behaviors it exercises;
- a small architecture/data-flow diagram;
- API request/signature documentation with a reproducible local example that uses only synthetic data;
- updated screenshots and README boundaries;
- a release asset checksum.

Allowed claim after the gate passes:

> A provider-neutral synthetic `payment.failed` webhook is authenticated with HMAC, admitted idempotently under at-least-once delivery, and converted into one auditable case transaction. PostgreSQL 17 migrations and selected concurrency behavior are exercised in public CI.

Disallowed claims include “Stripe integration,” “Shopify integration,” “exactly once,” “production ready,” “high availability,” “all PostgreSQL behavior verified,” or any customer outcome.

## 15. Acceptance criteria

I04 is complete only when all of the following are true:

- the three scoped implementation pull requests are reviewed, merged, and linked from the public issue;
- existing SQLite gates remain green;
- the PostgreSQL job and container readiness proof are green on `main`;
- every signing, replay, tenant, transaction, and failure-order test in this design passes;
- the four simulator scenarios work in a real browser;
- one webhook-created case completes the existing Manager-to-Agent workflow;
- no raw payload, secret, signature, token, or personal data appears in the database, logs, browser bundle, repository history, or release assets;
- README, security model, design summary, API documentation, screenshots, and limitations match implemented behavior;
- v0.2.0 is tagged from the verified `main` SHA and its release assets are reachable anonymously;
- the X launch copy distinguishes synthetic evidence from real provider/customer work and provides a direct repository and release-video path.
