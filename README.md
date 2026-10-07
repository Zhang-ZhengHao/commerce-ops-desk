# CommerceOps Desk

CommerceOps Desk is a full-stack ecommerce operations workspace for triaging payment, refund, fulfillment, and webhook-processing exceptions.

The project is being built as a verifiable delivery case study: every public capability will link to runnable tests, CI evidence, a versioned release, and a live demo using synthetic data.

## Planned workflow

```text
signed webhook
  -> immutable event and transactional outbox
  -> deterministic exception detection
  -> manager assignment
  -> agent investigation and resolution
  -> append-only audit timeline
  -> retry, dead-letter, and manual reprocessing
```

## Engineering boundaries

- React and TypeScript client backed by a FastAPI API.
- PostgreSQL is the reference deployment database; the public single-node demo uses SQLite.
- Role-based access and tenant isolation are enforced by the backend.
- Webhook authenticity, event idempotency, retries, and recovery are first-class behavior.
- Demo orders, users, and incidents are synthetic. The application does not perform real refunds, fulfillment, or store changes.

The repository is currently in its private bootstrap phase. Implementation and evidence will be published only after the first runnable release gate passes.

See [the public design summary](docs/design-summary.md) for the approved scope and verification strategy.

## License

[MIT](LICENSE)
