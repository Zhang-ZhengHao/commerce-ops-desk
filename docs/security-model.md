# CommerceOps Desk Security Model

This document describes controls implemented through the I04 signed-webhook
simulator slice, the `v0.2.1` evaluator candidate, and the limits of the hosted
synthetic demo. It is an engineering boundary, not a compliance claim, a
deployment attestation, or a substitute for an independent production review.

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

## Evaluator access, identity, and free text

- The release contract requires any externally shared evaluator to remain
  behind the enterprise access-code gateway. The gateway, not CommerceOps,
  owns that challenge. The application adds no access-code input, cookie,
  environment variable, API route, or repository setting, and the code must
  not appear in source, commands, logs, screenshots, or release notes.
- The enterprise access code must never be stored in or published through this
  repository.
- This checkpoint does not add or publish a live-demo CTA, and it does not
  claim that the evaluator is deployed, released, or approved for external
  access.
- The exact deployment hostname already exists in versioned engineering files
  and public Git history, so repository URL absence is not a release boundary.
  The actual publication gates are enterprise access-code distribution and
  promotion of the deployment as a live evaluator. Access-code sharing
  requires separate administrator confirmation of scope plus revocation and
  rotation ownership.
- The unauthenticated `GET /api/build` endpoint returns exactly the fixed
  service identifier, semantic version, and a full lowercase 40-character
  source SHA or `null`. It exposes no environment dump, path, image ID, secret,
  integration identifier, session, or user data and sends `Cache-Control: no-store`.
- Verified images bind the same approved SHA to the OCI revision label and the
  immutable runtime build identity. The client validates the complete response
  and constructs a commit link from a fixed repository origin; malformed or
  unavailable metadata cannot block operational UI. A `null` SHA identifies an
  unverified local build, not an accepted evaluator image.
- All supplied records are synthetic, but internal notes are evaluator-entered
  free text. The UI therefore states: `Use fictional text only. Do not enter personal, customer, credential, or confidential data.` The note textarea is
  programmatically associated with that warning. There is no content scanner,
  so the evaluator remains responsible for complying with it.

Live evaluator: single-node SQLite; PostgreSQL 17: CI-verified path only

This is a mandatory disclosure of the database and evidence boundary, not a
claim of multi-node operation, public availability, production readiness, or
an SLA.

## Signed webhook boundary

- The Manager-only signing route is registered only outside production when both demo mode and webhook intake are enabled; it is not registered in production. It requires the existing authenticated Manager session, exact same-origin check, and CSRF token before its closed `fresh` or `stale` body is validated.
- The server chooses the target path, tenant integration, event and order identifiers, timestamp, and exact JSON body. `fresh` uses the current time; `stale` is exactly 301 seconds old. The response is marked `Cache-Control: no-store` and never returns a master or derived key.
- In the browser, the complete returned envelope stays in panel-local memory and never enters storage, URLs, logs, or error objects. The UI renders only the allowlisted external event ID; the target path, timestamp, signature, and raw body are not rendered. Delivery to the public webhook route omits cookies and all browser credentials. Exact replay reuses the cached envelope, tamper changes a copy after signing, and stale testing cannot overwrite the fresh replay cache.
- Webhook intake is registered only when enabled with a dedicated master secret of at least 32 UTF-8 bytes. Local settings and production default it off; the hosted demo launcher enables it and creates or reuses an isolated mode-`0600` secret file. It never reuses the browser session secret.
- The route identifier must be a canonical lowercase UUID in the exact raw ASCII path. Security headers must each occur exactly once and use closed timestamp, event-ID, and signature grammars.
- A per-integration key is derived from the master secret, canonical integration ID, and positive key version. HMAC-SHA256 covers the version marker, timestamp, integration ID, event ID, and exact raw body bytes. Verification happens before media-type or JSON inspection, and comparison is constant-time.
- Timestamps exactly 300 seconds in the past or future are accepted; values beyond that window fail. Retrying a committed event uses the same event ID and body with a current timestamp and signature.
- Organization identity comes only from the authenticated integration join. The payload cannot select a tenant. Unknown, disabled, non-demo, expired, malformed, and version-rotated targets perform HMAC work and share the same compact `401` response.
- The closed payload accepts one synthetic `payment.failed` event shape. It rejects extra customer fields, duplicate JSON keys, non-standard numeric constants, invalid UTF-8, a BOM, trailing data, compression, and unsupported content types.
- The service stores a SHA-256 digest of the exact body, constrained event metadata, and safe result references. It never stores the body, signature, derived key, master secret, request headers, source address, session token, or arbitrary customer text.
- Inbox claim, demo allowance, order snapshot, payment state, case, system audit, and result references commit once in one business transaction. Exact body replays return the stored case without consuming allowance; reuse of an event ID with a different valid raw-body encoding or an immutable order mismatch returns `409`.
- A `503` may mean the commit result was not observed. The service does not retry internally; the sender safely retries the same event ID and body with fresh authentication material. The receiver safely supports retries from an at-least-once sender; it does not claim exactly-once delivery.
- The browser treats a delivery success followed by a read failure as a committed delivery. Its recovery action repeats only dashboard, queue, and case-detail GET requests; it never resends that webhook. A network error or `503` remains an unknown outcome and is not automatically retried.
- Safe case provenance is tenant-scoped and exposes only source kind plus, for synthetic events, provider, event type, constrained external event ID, and receipt time. It never exposes an integration ID, payload digest, signature, headers, raw body, or internal inbox identifier.

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

All included identities and order records are fictional. No customer email, address, payment instrument, merchant credential, or live provider token is required or stored. Because visitors control internal-note text, this boundary depends on the visible fictional-text rule described above rather than technical content detection.

The hosted synthetic demo is single-node. SQLite WAL is used only on a host-local path; tests explicitly avoid the NFS-mounted workspace after reproducing lock stalls there. The direct launcher keeps its default database in disposable local runtime storage. The workstation deployment instead bind-mounts the database and generated secret files so they survive container replacement; backups and high availability remain outside this demo's scope.

The workstation Caddy helper maintains a root-owned, canonical, linear route
head and permits rollback only from the current head's immutable schema-3
transaction. Target health and full upstream identity are checked before a
route can be exposed; route installation, marker verification, and a second
identity verification finish before the head advances. The site file and head
file are still separate replacements: a process killed between them can leave
the site ahead of the head. A lock-protected reconcile command accepts only the
exact direct-child transaction, fully revalidates either recognized route, and
then advances or preserves the head as appropriate. Before the head advances,
a failed orphan-route verification can restore the trusted backup only after
the parent, site, backup target, and restored route are all revalidated. The
human deployment operator is part of the trusted computing base because
privileged helper code comes from that operator's checkout; a malicious
operator and a privileged writer that ignores the shared lock remain outside
this boundary. This is
deployment integrity evidence, not a claim of cross-file atomicity, disaster
recovery, or protection from root.

Application shutdown gives the maintenance scheduler five seconds by default to finish and then cancels the scheduler coroutine so the FastAPI lifespan can close. Python cannot forcibly terminate a synchronous database call that has already been dispatched through `asyncio.to_thread`; that call may finish after the lifespan timeout, and its Session remains responsible for closing its checked-out connection. Engine disposal releases the application's idle pool without claiming that the worker thread was killed. The timeout is configurable through `COMMERCE_OPS_MAINTENANCE_SHUTDOWN_TIMEOUT_SECONDS` and must be finite and positive.

PostgreSQL 17 migrations, tenant constraints, readiness, selected
lock/concurrency behavior, webhook transactions, and the production container
path are exercised against a live disposable database in CI. This does not
establish multi-node throughput or availability. Edge bandwidth protection,
distributed rate limiting, overlapping-key rotation, a real Stripe or Shopify
integration, an asynchronous job queue or outbox/worker, automatic delivery
retries, a dead-letter queue, exactly-once delivery, encrypted backups,
production observability, disaster recovery, high availability, production
readiness, a production SLA, performance claims, and formal compliance remain
outside this revision.
