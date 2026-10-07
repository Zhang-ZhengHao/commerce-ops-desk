# CommerceOps Desk — Design Summary

## Current verified slice

I01 establishes the hosted foundation and nothing beyond it:

- FastAPI configuration, SQLite/PostgreSQL driver validation, liveness, and database readiness.
- An Alembic lineage that upgrades a fresh SQLite database before startup.
- Same-origin delivery of a responsive React entry shell with explicit synthetic-demo boundaries.
- Component tests plus desktop and mobile Playwright smoke coverage with browser-error guards.
- Locked setup, hosted-start, offline runtime-recovery, and public-history scan contracts.

Manager and Agent buttons are intentionally disabled. Identity, authorization, webhook, worker, case-management, and recovery behavior described below is planned, not current.

## Target product outcome (planned)

CommerceOps Desk is intended to give a small ecommerce operations team one place to inspect and resolve exceptions caused by payment failures, refund review, delayed fulfilment, and event-processing failures.

The primary demonstration will follow one complete path:

1. A server-signed synthetic webhook enters the external-integration route.
2. The API authenticates, validates, deduplicates, and persists the event with an outbox job in one transaction.
3. A worker applies deterministic rules and opens an exception case.
4. A manager assigns the case to an agent.
5. The agent investigates, adds a note, and submits an allowed resolution.
6. The audit timeline explains every material action.
7. A separate failure scenario demonstrates retries, dead-lettering, recovery, and creation of a new reprocessing job.

## Planned users and authorization

The public demo is designed to offer temporary Manager and Agent identities without registration.

- Managers will inspect organization-wide work, assign cases, run synthetic scenarios, recover demo faults, and reprocess dead jobs.
- Agents will see and update only cases assigned to them.
- Tenant scope and record visibility will be enforced on the server; cross-organization records will appear as not found.
- Cookie-authenticated writes will require CSRF protection and an idempotency key.

Demo workspaces are planned to expire after four hours and contain only synthetic identities and order data.

## Target system boundaries

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

The intended boundary keeps business rules out of HTTP handlers, avoids duplicating authorization rules in the UI, and gives the API and worker explicit database contracts.

## Planned reliability model

- Accepted webhook events will be immutable.
- Event and initial outbox-job creation will share one transaction.
- Worker delivery will be at least once; stable database keys will make business effects idempotent.
- PostgreSQL workers will claim jobs with leases and `FOR UPDATE SKIP LOCKED`.
- The SQLite demo will run one worker with WAL, a busy timeout, and conditional claiming.
- Failed executions will create attempt records and follow an explicit backoff policy.
- Manual reprocessing will preserve the original event, job, and attempts and create a new linked job.

No feature will be described as exactly once, highly available, or connected to a real store.

## Security and privacy boundaries

Later slices plan raw-request limits, timestamp windows, HMAC-SHA256 verification, constant-time comparison, server-enforced tenant scope, CSRF protection, and rate limits.

The current public-history scanner detects recognized credential formats, high-confidence secret assignments, personal email addresses, internal workspace paths, cluster-local hostnames, and RFC1918 addresses. It is a guardrail, not proof that arbitrary customer data is absent; release review still requires synthetic fixtures and human inspection.

## Verification roadmap

Current I01 evidence covers backend unit/API/configuration tests, an empty-SQLite migration proof, frontend component tests, same-origin hosted smoke tests, desktop/mobile browser smoke tests, strict type and lint checks, and public-history scanning.

Later milestones will add authorization and tenant-isolation tests, webhook and worker integration tests, generated API type consistency, PostgreSQL jobs, Compose checks, the complete Manager-to-Agent flow, recovery scenarios, versioned releases, screenshots, and a released-build walkthrough. Those checks are not claimed as current CI evidence.
