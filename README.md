# CommerceOps Desk

CommerceOps Desk is an in-progress full-stack case study for triaging ecommerce payment, refund, fulfilment, and event-processing exceptions. It is built in public-facing slices: a capability is described as complete only when its code, runnable tests, and operating evidence exist.

All demo data is synthetic. The application does not connect to a store or perform real refunds, fulfilment changes, or payment actions.

## Current verified scope

**I01 — Hosted foundation** is implemented and locally verified:

- FastAPI starts from an injected `PORT`, binds to `0.0.0.0`, and serves the Vite production build from the same origin.
- `/health` reports process liveness; `/ready` checks the configured SQLite or PostgreSQL connection.
- Alembic upgrades a fresh SQLite database to the `0001_foundation` revision before the server accepts traffic.
- The responsive React entry shell explains the product and public-demo boundaries; unfinished Manager and Agent entries are visibly disabled.
- Playwright exercises the foundation on desktop and mobile while failing on page errors, console errors, or unexpected HTTP failures.
- A publish gate scans the worktree, index, and reachable Git history for recognized secrets and internal identifiers without echoing matched values.
- Python and Node dependencies are locked, and the hosted Python runtime can be rebuilt from a prepared wheelhouse without network access.

Accounts, RBAC, tenant isolation, webhook ingestion, outbox processing, case resolution, retries, and recovery are **not implemented yet**. They remain planned slices and are not represented as current behavior.

## Run locally

The supported development path expects Linux, Bash, GNU Make, Python 3.12, Node 22, and npm.

```bash
cp .env.example .env
make setup
make run
```

The safe example configuration starts at `http://localhost:8000` with a local SQLite database. `make run` builds the frontend and loads `.env`; if `.env` is absent, it uses the non-secret defaults in `.env.example`.

Run the complete I01 gate with:

```bash
make verify
```

Individual targets include `backend-test`, `frontend-test`, `sqlite-migrate-test`, `hosted-smoke`, `e2e-smoke`, `public-scan`, `lint`, and `build`.

## Planned product workflow

```text
signed synthetic webhook
  -> immutable event and transactional outbox
  -> deterministic exception detection
  -> manager assignment
  -> agent investigation and resolution
  -> append-only audit timeline
  -> retry, dead-letter, and manual reprocessing
```

The target architecture uses a React and TypeScript client, a FastAPI API, SQLAlchemy, a database outbox worker, PostgreSQL as the reference deployment database, and SQLite for the public single-node demo. Each of those later capabilities must pass its own release gate before it moves into the verified scope above.

See the [design summary](docs/design-summary.md) for the product boundaries, reliability model, and verification roadmap.

## Repository status

The repository remains in private bootstrap until the first runnable release gate is complete. No production usage, customer outcome, availability, or scale claim is made.

## License

[MIT](LICENSE)
