# CommerceOps Desk — Design Summary

## Product outcome

CommerceOps Desk gives a small ecommerce operations team one place to inspect and resolve exceptions caused by payment failures, refund review, delayed fulfillment, and event-processing failures.

The primary demonstration follows one complete path:

1. A server-signed synthetic webhook enters the same ingestion route used by external integrations.
2. The API authenticates, validates, deduplicates, and persists the event with an outbox job in one transaction.
3. A worker applies deterministic rules and opens an exception case.
4. A manager assigns the case to an agent.
5. The agent investigates, adds a note, and submits an allowed resolution.
6. The audit timeline explains every material action.
7. A separate failure scenario demonstrates retries, dead-lettering, recovery, and creation of a new reprocessing job.

## Users and authorization

The public demo offers temporary Manager and Agent identities without registration.

- Managers can inspect organization-wide work, assign cases, run synthetic scenarios, recover demo faults, and reprocess dead jobs.
- Agents can see and update only cases assigned to them.
- Tenant scope and record visibility are enforced on the server. Cross-organization records appear as not found.
- Cookie-authenticated writes require CSRF protection and an idempotency key.

Demo workspaces expire after four hours and contain only synthetic identities and order data.

## System boundaries

```text
React + TypeScript
        |
FastAPI HTTP API and static delivery
        |
SQLAlchemy domain and repositories
        |
PostgreSQL reference deployment / SQLite single-node demo
        |
Database outbox worker
```

The HTTP layer does not own business rules, the UI does not duplicate authorization rules, and the API and worker communicate through explicit database contracts.

## Reliability model

- Accepted webhook events are immutable.
- Event and initial outbox job creation share one transaction.
- Worker delivery is at least once; stable database keys make business effects idempotent.
- PostgreSQL workers claim jobs with leases and `FOR UPDATE SKIP LOCKED`.
- The SQLite demo runs one worker with WAL, a busy timeout, and conditional claiming.
- Failed executions create attempt records and follow an explicit backoff policy.
- Manual reprocessing preserves the original event, job, and attempts and creates a new linked job.

No feature is described as exactly once, highly available, or connected to a real store.

## Security and privacy

- Raw request limits, timestamp windows, HMAC-SHA256, and constant-time comparison protect webhook intake.
- Integration secrets are referenced rather than returned through normal API responses or logs.
- Logs exclude cookies, signatures, customer names, payment data, and raw source addresses by default.
- Demo rate limits cap workspace creation, simulated events, notes, and webhook intake.
- Public-history scans block secrets, private email addresses, real customer data, and internal infrastructure references.

## Verification strategy

Evidence is part of the product rather than an afterthought:

- Backend unit, API, authorization, migration, and database integration tests.
- Frontend component tests and generated OpenAPI type consistency.
- Playwright coverage for the main Manager-to-Agent workflow and recovery states.
- SQLite checks locally and PostgreSQL/Compose checks on GitHub Actions.
- Hosted and deployed smoke tests tied to an exact commit SHA.
- Versioned releases, screenshots, and a short walkthrough based on the released build.

The public repository will distinguish the SQLite demo from the PostgreSQL reference deployment and will not claim production usage or customer outcomes that have not occurred.
